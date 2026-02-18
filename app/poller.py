import time
import threading
from app import db, gmail_client, llm_client

_stop_event = threading.Event()
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
        interval = int(db.get_setting("poll_interval", "300"))
        _status["next_run"] = time.time() + interval
        _stop_event.wait(timeout=interval)
    _status["running"] = False


def _scan_all_accounts():
    _status["last_run"] = time.time()
    accounts = [a for a in db.list_accounts() if a["active"]]
    prompts = [p for p in db.list_prompts() if p["active"]]

    if not accounts:
        db.add_log("INFO", "Poller ran: no active accounts configured.")
        return
    if not prompts:
        db.add_log("INFO", "Poller ran: no active prompts configured.")
        return

    db.add_log("INFO", f"Starting scan: {len(accounts)} account(s), {len(prompts)} prompt(s).")

    for account in accounts:
        _scan_account(account, prompts)


def _scan_account(account, prompts):
    account_id = account["id"]
    email_addr = account["email"]
    try:
        service, refreshed_creds = gmail_client.get_service(account["credentials_json"])
        if refreshed_creds != account["credentials_json"]:
            db.update_account_credentials(account_id, refreshed_creds)

        emails = gmail_client.fetch_recent_emails(service)
        new_emails = [e for e in emails if not db.is_processed(account_id, e["id"])]

        if not new_emails:
            db.add_log("INFO", f"[{email_addr}] No new emails to process.")
            db.update_last_scan(account_id)
            return

        db.add_log("INFO", f"[{email_addr}] Processing {len(new_emails)} new email(s).")

        label_cache = {}
        for prompt in prompts:
            if prompt["label_name"] not in label_cache:
                label_cache[prompt["label_name"]] = gmail_client.get_or_create_label(
                    service, prompt["label_name"]
                )

        for email in new_emails:
            for prompt in prompts:
                try:
                    if llm_client.should_apply_label(email, prompt["instructions"]):
                        gmail_client.apply_label(service, email["id"], label_cache[prompt["label_name"]])
                        db.add_log(
                            "INFO",
                            f"[{email_addr}] Labeled '{email['subject'][:60]}' → {prompt['label_name']} (rule: {prompt['name']})",
                        )
                    else:
                        db.add_log(
                            "DEBUG",
                            f"[{email_addr}] Skipped '{email['subject'][:60]}' for rule: {prompt['name']}",
                        )
                except Exception as e:
                    db.add_log("ERROR", f"[{email_addr}] LLM error on '{email['subject'][:60]}': {e}")

            db.mark_processed(account_id, email["id"])

        db.update_last_scan(account_id)

    except Exception as e:
        db.add_log("ERROR", f"[{email_addr}] Scan failed: {e}")
