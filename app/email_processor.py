from app import db, gmail_client, llm
from app.config import GMAIL_LOOKBACK_HOURS, GMAIL_MAX_RESULTS
from app.gmail_client import LABEL_INBOX, LABEL_SPAM, LABEL_UNREAD
from app.models import CategorizationHistory, Log, ProcessedEmail, database


def process_account(account: dict, prompts: list):
    """Fetch new emails for an account, classify them, and apply labels/actions.
    Always returns credentials so the caller can run retention cleanup."""
    account_id = account["id"]
    email_addr = account["email"]

    service = gmail_client.get_service_and_refresh(account)

    all_ids = gmail_client.list_recent_message_ids(
        service, max_results=GMAIL_MAX_RESULTS, lookback_hours=GMAIL_LOOKBACK_HOURS
    )
    unprocessed_ids = db.filter_unprocessed(account_id, all_ids)

    if not unprocessed_ids:
        db.add_log("INFO", f"[{email_addr}] No new emails to process.")
        db.update_last_scan(account_id)
        return service

    new_emails = gmail_client.fetch_message_details(service, unprocessed_ids)
    db.add_log("INFO", f"[{email_addr}] Processing {len(new_emails)} new email(s) against {len(prompts)} rule(s).")

    unique_labels = list({p["label_name"] for p in prompts})
    label_cache = gmail_client.build_label_cache(service, unique_labels)

    all_modifies = []
    all_trashes = []

    for email in new_emails:
        modifies, trashes = _process_email(email, account_id, email_addr, prompts, label_cache)
        all_modifies.extend(modifies)
        all_trashes.extend(trashes)

    if all_trashes:
        gmail_client.batch_trash_emails(service, all_trashes)
    if all_modifies:
        gmail_client.batch_modify_emails(service, all_modifies)

    db.update_last_scan(account_id)
    return service


def _process_email(email: dict, account_id: int, email_addr: str, prompts: list, label_cache: dict) -> tuple:
    """Classify an email and write DB records. Returns (modifies, trashes) for batched Gmail calls."""
    modifies = []
    trashes = []
    try:
        email_results = llm.classify_email_batch(email, prompts)
        stop = False

        # Collect DB writes; apply all in one transaction.
        pending_logs = []
        pending_cats = []

        for prompt in prompts:
            if stop:
                break
            should_label = email_results.get(prompt["id"], False)

            if should_label:
                label_id = label_cache.get(prompt["label_name"])
                if label_id is None:
                    db.add_log(
                        "WARNING",
                        f"[{email_addr}] Label '{prompt['label_name']}' missing from cache, skipping rule '{prompt['name']}'",
                    )
                    continue
                add_labels = [label_id]
                remove_labels = []
                actions_taken = [f"labeled → {prompt['label_name']}"]
                use_trash = False

                if prompt.get("action_spam"):
                    add_labels.append(LABEL_SPAM)
                    remove_labels.append(LABEL_INBOX)
                    actions_taken.append("sent to spam")
                elif prompt.get("action_trash"):
                    use_trash = True
                    actions_taken.append("trashed")
                elif prompt.get("action_archive"):
                    remove_labels.append(LABEL_INBOX)
                    actions_taken.append("archived")

                if prompt.get("action_mark_read"):
                    remove_labels.append(LABEL_UNREAD)
                    actions_taken.append("marked as read")

                if use_trash:
                    trashes.append(email["id"])
                else:
                    modifies.append((email["id"], add_labels, remove_labels))

                if prompt.get("stop_processing"):
                    actions_taken.append("stopped further rules")
                    stop = True

                pending_logs.append(
                    (
                        "INFO",
                        f"[{email_addr}] '{email['subject'][:60]}' — {', '.join(actions_taken)} (rule: {prompt['name']})",
                    )
                )
                pending_cats.append(
                    {
                        "account_id": account_id,
                        "account_email": email_addr,
                        "message_id": email["id"],
                        "subject": email.get("subject", ""),
                        "sender": email.get("sender", ""),
                        "prompt_id": prompt["id"],
                        "prompt_name": prompt["name"],
                        "label_name": prompt["label_name"],
                        "actions": ", ".join(actions_taken),
                    }
                )

        with database.atomic():
            Log.insert_many([{"level": lvl, "message": msg} for lvl, msg in pending_logs]).execute()
            if pending_cats:
                CategorizationHistory.insert_many(pending_cats).execute()
            ProcessedEmail.insert(account_id=account_id, message_id=email["id"]).on_conflict_ignore().execute()
    except Exception as e:
        db.add_log("ERROR", f"[{email_addr}] Error processing email '{email.get('subject', '?')[:60]}': {e}")
        db.mark_processed(account_id, email["id"])  # prevent infinite retry on persistent failures
        return [], []

    return modifies, trashes
