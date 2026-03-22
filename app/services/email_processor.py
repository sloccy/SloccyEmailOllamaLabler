from app import db, gmail_client
from app.db import get_db
from app.llm.base import LLMProvider
from app.config import GMAIL_MAX_RESULTS, GMAIL_LOOKBACK_HOURS


def process_account(account: dict, prompts: list, provider: LLMProvider):
    """Fetch new emails for an account, classify them, and apply labels/actions.
    Always returns credentials so the caller can run retention cleanup."""
    account_id = account["id"]
    email_addr = account["email"]

    creds, refreshed_creds = gmail_client.get_service(account["credentials_json"])
    if refreshed_creds != account["credentials_json"]:
        db.update_account_credentials(account_id, refreshed_creds)

    all_ids = gmail_client.list_recent_message_ids(
        creds, max_results=GMAIL_MAX_RESULTS, lookback_hours=GMAIL_LOOKBACK_HOURS
    )
    unprocessed_ids = db.filter_unprocessed(account_id, all_ids)

    if not unprocessed_ids:
        db.add_log("INFO", f"[{email_addr}] No new emails to process.")
        db.update_last_scan(account_id)
        return creds

    new_emails = gmail_client.fetch_message_details(creds, unprocessed_ids)
    db.add_log("INFO", f"[{email_addr}] Processing {len(new_emails)} new email(s) against {len(prompts)} rule(s).")

    unique_labels = list({p["label_name"] for p in prompts})
    label_cache = gmail_client.build_label_cache(creds, unique_labels)

    for email in new_emails:
        _process_email(email, account_id, email_addr, prompts, label_cache, creds, provider)

    db.update_last_scan(account_id)
    return creds


def _process_email(email: dict, account_id: int, email_addr: str,
                   prompts: list, label_cache: dict, creds, provider: LLMProvider) -> None:
    try:
        email_results = provider.classify_email_batch(email, prompts)
        stop = False

        # Collect Gmail API calls and DB writes; apply all DB writes in one transaction.
        pending_logs = []
        pending_cats = []

        for prompt in prompts:
            if stop:
                break
            should_label = email_results.get(prompt["id"], False)

            if should_label:
                add_labels = [label_cache[prompt["label_name"]]]
                remove_labels = []
                actions_taken = [f"labeled → {prompt['label_name']}"]
                use_trash = False

                if prompt.get("action_spam"):
                    add_labels.append("SPAM")
                    remove_labels.append("INBOX")
                    actions_taken.append("sent to spam")
                elif prompt.get("action_trash"):
                    use_trash = True
                    actions_taken.append("trashed")
                elif prompt.get("action_archive"):
                    remove_labels.append("INBOX")
                    actions_taken.append("archived")

                if prompt.get("action_mark_read"):
                    remove_labels.append("UNREAD")
                    actions_taken.append("marked as read")

                if use_trash:
                    gmail_client.trash_email(creds, email["id"])
                else:
                    gmail_client.modify_email(creds, email["id"], add_labels, remove_labels)

                if prompt.get("stop_processing"):
                    actions_taken.append("stopped further rules")
                    stop = True

                pending_logs.append((
                    "INFO",
                    f"[{email_addr}] '{email['subject'][:60]}' — {', '.join(actions_taken)} (rule: {prompt['name']})",
                ))
                pending_cats.append({
                    "account_id": account_id,
                    "account_email": email_addr,
                    "message_id": email["id"],
                    "subject": email.get("subject", ""),
                    "sender": email.get("sender", ""),
                    "prompt_id": prompt["id"],
                    "prompt_name": prompt["name"],
                    "label_name": prompt["label_name"],
                    "actions": ", ".join(actions_taken),
                })

        with get_db() as conn:
            for level, message in pending_logs:
                conn.execute(
                    "INSERT INTO logs (level, message) VALUES (?, ?)", (level, message)
                )
            for c in pending_cats:
                conn.execute(
                    """INSERT INTO categorization_history
                       (account_id, account_email, message_id, subject, sender,
                        prompt_id, prompt_name, label_name, actions)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (c["account_id"], c["account_email"], c["message_id"], c["subject"],
                     c["sender"], c["prompt_id"], c["prompt_name"], c["label_name"], c["actions"]),
                )
            conn.execute(
                "INSERT OR IGNORE INTO processed_emails (account_id, message_id) VALUES (?, ?)",
                (account_id, email["id"]),
            )
    except Exception as e:
        db.add_log("ERROR", f"[{email_addr}] Error processing email '{email.get('subject', '?')[:60]}': {e}")
        db.mark_processed(account_id, email["id"])  # prevent infinite retry on persistent failures
