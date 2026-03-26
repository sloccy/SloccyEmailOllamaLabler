from datetime import UTC, datetime, timedelta

from peewee import fn

from app.config import LOG_RETENTION_DAYS, POLL_INTERVAL
from app.models import (
    ALL_MODELS,
    DB_PATH,
    Account,
    AccountRetention,
    CategorizationHistory,
    LabelExemption,
    LabelRetention,
    Log,
    ProcessedEmail,
    Prompt,
    Setting,
    database,
)


def init_db():
    database.init(DB_PATH, pragmas={"journal_mode": "wal"}, check_same_thread=False)
    database.connect()
    database.create_tables(ALL_MODELS, safe=True)
    Setting.insert(key="poll_interval", value=str(POLL_INTERVAL)).on_conflict_ignore().execute()


# ---- Settings ----


def get_setting(key, default=None):
    row = Setting.get_or_none(Setting.key == key)
    return row.value if row else default


def set_setting(key, value):
    Setting.replace(key=key, value=str(value)).execute()


def get_all_settings():
    return list(Setting.select().dicts())


# ---- Accounts ----


def list_accounts():
    return list(Account.select().order_by(Account.added_at.desc()).dicts())


def list_accounts_safe():
    return list(
        Account.select(Account.id, Account.email, Account.added_at, Account.last_scan_at, Account.active)
        .order_by(Account.added_at.desc())
        .dicts()
    )


def get_account(account_id):
    return Account.select().where(Account.id == account_id).dicts().first()


def upsert_account(email, credentials_json):
    Account.insert(email=email, credentials_json=credentials_json).on_conflict(
        conflict_target=[Account.email],
        update={Account.credentials_json: credentials_json, Account.active: 1},
    ).execute()


def update_account_credentials(account_id, credentials_json):
    Account.update(credentials_json=credentials_json).where(Account.id == account_id).execute()


def update_last_scan(account_id):
    Account.update(last_scan_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")).where(
        Account.id == account_id
    ).execute()


def delete_account(account_id):
    with database.atomic():
        Prompt.delete().where(Prompt.account_id == account_id).execute()
        CategorizationHistory.delete().where(CategorizationHistory.account_id == account_id).execute()
        AccountRetention.delete().where(AccountRetention.account_id == account_id).execute()
        LabelRetention.delete().where(LabelRetention.account_id == account_id).execute()
        LabelExemption.delete().where(LabelExemption.account_id == account_id).execute()
        ProcessedEmail.delete().where(ProcessedEmail.account_id == account_id).execute()
        Account.delete_by_id(account_id)


def _toggle_active(model, row_id):
    obj = model.get_or_none(model.id == row_id)
    if obj is None:
        return None
    new_active = 1 - obj.active
    model.update(active=new_active).where(model.id == row_id).execute()
    return new_active


def toggle_account(account_id):
    return _toggle_active(Account, account_id)


# ---- Prompts ----


def list_prompts(account_id=None):
    q = Prompt.select().order_by(Prompt.sort_order.asc(), Prompt.id.asc())
    if account_id is not None:
        q = q.where((Prompt.account_id == account_id) | Prompt.account_id.is_null())
    return list(q.dicts())


def get_prompt(prompt_id):
    return Prompt.select().where(Prompt.id == prompt_id).dicts().first()


def create_prompt(
    name,
    instructions,
    label_name,
    action_archive=0,
    action_spam=0,
    action_trash=0,
    action_mark_read=0,
    stop_processing=0,
    account_id=None,
):
    max_order = Prompt.select(fn.MAX(Prompt.sort_order)).scalar() or 0
    Prompt.create(
        name=name,
        instructions=instructions,
        label_name=label_name,
        action_archive=action_archive,
        action_spam=action_spam,
        action_trash=action_trash,
        action_mark_read=action_mark_read,
        sort_order=max_order + 1,
        stop_processing=stop_processing,
        account_id=account_id,
    )


def update_prompt(
    prompt_id,
    name,
    instructions,
    label_name,
    active,
    action_archive=0,
    action_spam=0,
    action_trash=0,
    action_mark_read=0,
    stop_processing=0,
    account_id=None,
):
    Prompt.update(
        name=name,
        instructions=instructions,
        label_name=label_name,
        active=active,
        action_archive=action_archive,
        action_spam=action_spam,
        action_trash=action_trash,
        action_mark_read=action_mark_read,
        stop_processing=stop_processing,
        account_id=account_id,
    ).where(Prompt.id == prompt_id).execute()


def toggle_prompt(prompt_id):
    return _toggle_active(Prompt, prompt_id)


def reorder_prompts(ordered_ids):
    with database.atomic():
        for i, pid in enumerate(ordered_ids):
            Prompt.update(sort_order=i + 1).where(Prompt.id == pid).execute()


def delete_prompt(prompt_id):
    Prompt.delete_by_id(prompt_id)


# ---- Processed emails ----


def filter_unprocessed(account_id, message_ids):
    if not message_ids:
        return []
    processed = {
        r["message_id"]
        for r in ProcessedEmail.select(ProcessedEmail.message_id)
        .where(ProcessedEmail.account_id == account_id, ProcessedEmail.message_id.in_(message_ids))
        .dicts()
    }
    return [mid for mid in message_ids if mid not in processed]


def mark_processed(account_id, message_id):
    ProcessedEmail.insert(account_id=account_id, message_id=message_id).on_conflict_ignore().execute()


def trim_processed_emails(lookback_hours):
    cutoff = (datetime.now(UTC) - timedelta(hours=lookback_hours * 2)).strftime("%Y-%m-%d %H:%M:%S")
    ProcessedEmail.delete().where(
        ProcessedEmail.processed_at.is_null(False),
        ProcessedEmail.processed_at < cutoff,
    ).execute()


# ---- Logs ----


def add_log(level, message):
    Log.create(level=level.upper(), message=message)


def trim_logs():
    retention_days = int(get_setting("log_retention_days", str(LOG_RETENTION_DAYS)))
    if retention_days > 0:
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).strftime("%Y-%m-%d %H:%M:%S")
        Log.delete().where(Log.timestamp < cutoff).execute()


def count_active_prompts():
    return Prompt.select().where(Prompt.active == 1).count()


def get_logs(limit=100):
    return list(Log.select().order_by(Log.id.desc()).limit(limit).dicts())


def get_logs_range(start, end):
    return list(Log.select().where(Log.timestamp >= start, Log.timestamp <= end).order_by(Log.id.asc()).dicts())


# ---- Categorization History ----


def get_categorization_history(account_id=None, prompt_id=None, subject=None, sender=None, limit=200):
    q = CategorizationHistory.select()
    if account_id is not None:
        q = q.where(CategorizationHistory.account_id == account_id)
    if prompt_id is not None:
        q = q.where(CategorizationHistory.prompt_id == prompt_id)
    if subject:
        q = q.where(CategorizationHistory.subject.contains(subject))
    if sender:
        q = q.where(CategorizationHistory.sender.contains(sender))
    return list(q.order_by(CategorizationHistory.id.desc()).limit(limit).dicts())


# ---- Retention Rules ----


def get_retention(account_id):
    ret = AccountRetention.get_or_none(AccountRetention.account_id == account_id)
    labels = list(
        LabelRetention.select(LabelRetention.id, LabelRetention.label_name, LabelRetention.days)
        .where(LabelRetention.account_id == account_id)
        .order_by(LabelRetention.id.asc())
        .dicts()
    )
    exemptions = list(
        LabelExemption.select(LabelExemption.id, LabelExemption.label_name)
        .where(LabelExemption.account_id == account_id)
        .order_by(LabelExemption.label_name.asc())
        .dicts()
    )
    return {
        "global_days": ret.global_days if ret else None,
        "labels": labels,
        "exemptions": exemptions,
    }


def set_global_retention(account_id, days):
    AccountRetention.replace(account_id=account_id, global_days=days).execute()


def clear_global_retention(account_id):
    AccountRetention.delete().where(AccountRetention.account_id == account_id).execute()


def add_label_retention(account_id, label_name, days):
    LabelRetention.replace(account_id=account_id, label_name=label_name, days=days).execute()


def delete_label_retention(rule_id):
    LabelRetention.delete_by_id(rule_id)


def add_label_exemption(account_id, label_name):
    LabelExemption.insert(account_id=account_id, label_name=label_name).on_conflict_ignore().execute()


def delete_label_exemption(exemption_id):
    LabelExemption.delete_by_id(exemption_id)


# ---- Import helpers ----


def create_account_placeholder(email):
    Account.insert(email=email, credentials_json="", active=1).on_conflict_ignore().execute()
    row = Account.select(Account.id).where(Account.email == email).dicts().first()
    return row["id"] if row else None


def prompt_exists(name, account_id):
    q = Prompt.select().where(Prompt.name == name)
    q = q.where(Prompt.account_id.is_null()) if account_id is None else q.where(Prompt.account_id == account_id)
    return q.exists()


def label_retention_exists(account_id, label_name):
    return (
        LabelRetention.select()
        .where(LabelRetention.account_id == account_id, LabelRetention.label_name == label_name)
        .exists()
    )


def has_global_retention(account_id):
    return AccountRetention.select().where(AccountRetention.account_id == account_id).exists()
