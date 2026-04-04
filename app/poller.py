import threading
import time

from app import db
from app.config import GMAIL_LOOKBACK_HOURS, POLL_INTERVAL
from app.email_processor import process_account
from app.retention import cleanup_retention

_scan_lock = threading.Lock()
_stop = threading.Event()
_interval: int = POLL_INTERVAL
_last_run: float | None = None
_last_cleanup: float = 0.0
_next_run: float | None = None
_running: bool = False
_CLEANUP_INTERVAL = 3600


def get_status() -> dict:
    return {
        "running": _running,
        "last_run": _last_run,
        "next_run": _next_run,
    }


def start() -> None:
    global _running, _interval, _next_run
    if _running:
        return
    _interval = int(db.get_setting("poll_interval", str(POLL_INTERVAL)))
    _next_run = time.time()
    _running = True
    threading.Thread(target=_loop, daemon=True).start()


def run_now() -> None:
    threading.Thread(target=_run_scan, daemon=True).start()


def update_interval(seconds: int) -> None:
    global _interval, _next_run
    _interval = seconds
    if _last_run is not None:
        _next_run = _last_run + seconds


def _loop() -> None:
    global _next_run
    while not _stop.wait(1.0):
        if _next_run is not None and time.time() >= _next_run:
            _run_scan()
            _next_run = time.time() + _interval


def _run_scan() -> None:
    if not _scan_lock.acquire(blocking=False):
        db.add_log("DEBUG", "Scan skipped: previous scan still running.")
        return
    try:
        global _last_run, _last_cleanup
        _last_run = time.time()
        now = _last_run
        if now - _last_cleanup >= _CLEANUP_INTERVAL:
            db.trim_logs()
            db.trim_processed_emails(GMAIL_LOOKBACK_HOURS)
            db.trim_categorization_history()
            _last_cleanup = now

        accounts = [a for a in db.list_accounts() if a["active"]]
        if not accounts:
            db.add_log("INFO", "Poller ran: no active accounts configured.")
            return

        all_prompts = db.list_prompts()
        for account in accounts:
            prompts = [
                p for p in all_prompts if p["active"] and (p["account_id"] is None or p["account_id"] == account["id"])
            ]
            if not prompts:
                db.add_log("INFO", f"[{account['email']}] No active prompts for this account.")
                continue
            db.add_log("INFO", f"Starting scan: [{account['email']}] with {len(prompts)} prompt(s).")
            try:
                service = process_account(account, prompts)
                cleanup_retention(account, service)
            except Exception as e:
                db.add_log("ERROR", f"[{account['email']}] Scan failed: {e}")
    finally:
        _scan_lock.release()
