import json
import time
import threading
from app import db, gmail_client, llm_client
from app.config import GMAIL_MAX_RESULTS, GMAIL_LOOKBACK_HOURS, POLL_INTERVAL

_stop_event = threading.Event()
_scan_lock = threading.Lock()
_thread = None
_status = {"running": False, "last_run": None, "next_run": None}


def get_status():
    return dict(_status)


def start():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()


def stop():
    _stop_event.set()


def run_now():
    threading.Thread(target=_scan_all_accounts, daemon=True).start()


def _loop():
    _status["running"] = True
    while not _stop_event.is_set():
        _scan_all_accounts()
        interval = int(db.get_setting("poll_interval", str(POLL_INTERVAL)))
        _status["next_run"] = time.time() + interval
        _stop_event.wait(timeout=interval)
    _status["running"] = False


def _scan_all_accounts():
    if not _scan_lock.acquire(blocking=False):
        db.add_log("INFO", "Scan already in progress, skipping.")
        return
    try:
        _run_scan()
    finally:
        _scan_lock.release()


def _run_scan():
    _status["last_run"] = time.time()
    db.trim_logs()
    accounts = [a for a in db.list_accounts() if a["active"]]

    if not accounts:
        db.add_log("INFO", "Poller ran: no active accounts configured.")
        return

    for account in accounts:
        prompts = [p for p in db.list_prompts(account_id=account["id"]) if p["active"]]
        if not prompts:
            db.add_log("INFO", f"[{account['email']}] No active prompts for this account.")
            continue
        db.add_log("INFO", f"Starting scan: [{account['email']}] with {len(prompts)} prompt(s).")
        _scan_account(account, prompts)


def _scan_account(account, prompts):
    account_id = account["id"]
    email_addr = account["email"]
    try:
        service, refreshed_creds = gmail_client.get_service(account["credentials_json"])
        if json.loads(refreshed_creds) != json.loads(account["credentials_json"]):
            db.update_account_credentials(account_id, refreshed_creds)

        emails = gmail_client.fetch_recent_emails(service, max_results=GMAIL_MAX_RESULTS, lookback_hours=GMAIL_LOOKBACK_HOURS)
        new_emails = [e for e in emails if not db.is_processed(account_id, e["id"])]

        if not new_emails:
            db.add_log("INFO", f"[{email_addr}] No new emails to process.")
            db.update_last_scan(account_id)
            return

        db.add_log("INFO", f"[{email_addr}] Processing {len(new_emails)} new email(s) against {len(prompts)} rule(s).")

        # Fetch/create all label IDs with a single Gmail API call
        unique_labels = list({p["label_name"] for p in prompts})
        label_cache = gmail_client.build_label_cache(service, unique_labels)

        # Process emails one-by-one to respect Ollama concurrency limits
        # (Ollama may only allow 2 concurrent requests)
        for email in new_emails:
            try:
                # Get results for this single email
                email_results = llm_client.classify_email_batch(email, prompts)

                stop = False

                for prompt in prompts:
                    if stop:
                        break
                    prompt_id = prompt["id"]
                    should_label = email_results.get(prompt_id, False)

                    if should_label:
                        gmail_client.apply_label(service, email["id"], label_cache[prompt["label_name"]])
                        actions_taken = [f"labeled → {prompt['label_name']}"]

                        if prompt.get("action_spam"):
                            gmail_client.spam_email(service, email["id"])
                            actions_taken.append("sent to spam")
                        elif prompt.get("action_trash"):
                            gmail_client.trash_email(service, email["id"])
                            actions_taken.append("trashed")
                        elif prompt.get("action_archive"):
                            gmail_client.archive_email(service, email["id"])
                            actions_taken.append("archived")

                        if prompt.get("stop_processing"):
                            actions_taken.append("stopped further rules")
                            stop = True

                        db.add_log(
                            "INFO",
                            f"[{email_addr}] '{email['subject'][:60]}' — {', '.join(actions_taken)} (rule: {prompt['name']})",
                        )
                    else:
                        db.add_log(
                            "DEBUG",
                            f"[{email_addr}] Skipped '{email['subject'][:60]}' for rule: {prompt['name']}",
                        )

                db.mark_processed(account_id, email["id"])
            except Exception as e:
                db.add_log("ERROR", f"[{email_addr}] Error processing email: {e}")

        db.update_last_scan(account_id)

    except Exception as e:
        db.add_log("ERROR", f"[{email_addr}] Scan failed: {e}")
