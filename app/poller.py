import datetime
import time

from apscheduler.schedulers.background import BackgroundScheduler

from app import db
from app.config import GMAIL_LOOKBACK_HOURS, POLL_INTERVAL
from app.llm import get_provider
from app.services.email_processor import process_account
from app.services.retention import cleanup_retention

_scheduler = BackgroundScheduler(daemon=True)
_last_run: float | None = None
_last_cleanup = 0.0
_CLEANUP_INTERVAL = 3600


def get_status() -> dict:
    job = _scheduler.get_job("poll")
    next_run = job.next_run_time.timestamp() if job and job.next_run_time else None
    return {
        "running": _scheduler.running,
        "last_run": _last_run,
        "next_run": next_run,
    }


def start() -> None:
    if _scheduler.running:
        return
    interval = int(db.get_setting("poll_interval", str(POLL_INTERVAL)))
    _scheduler.add_job(
        _run_scan,
        "interval",
        seconds=interval,
        id="poll",
        max_instances=1,
        replace_existing=True,
        next_run_time=datetime.datetime.now(),
    )
    _scheduler.start()


def stop() -> None:
    _scheduler.shutdown(wait=False)


def run_now() -> None:
    _scheduler.add_job(_run_scan, id="manual_scan", max_instances=1, replace_existing=True)


def update_interval(seconds: int) -> None:
    if _scheduler.running:
        _scheduler.reschedule_job("poll", trigger="interval", seconds=seconds)


def _run_scan() -> None:
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

    provider = get_provider()
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
            service = process_account(account, prompts, provider)
            cleanup_retention(account, service)
        except Exception as e:
            db.add_log("ERROR", f"[{account['email']}] Scan failed: {e}")
