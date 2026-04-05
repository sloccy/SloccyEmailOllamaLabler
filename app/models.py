import os
from datetime import UTC, datetime

from peewee import AutoField, IntegerField, Model, SqliteDatabase, TextField

DB_PATH = os.path.join(os.getenv("DATA_DIR", "/data"), "labeler.db")

database = SqliteDatabase(None)


def _now():
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


class BaseModel(Model):
    class Meta:
        database = database


class Account(BaseModel):
    id = AutoField()
    email = TextField(unique=True)
    credentials_json = TextField()
    added_at = TextField(default=_now)
    last_scan_at = TextField(null=True)
    active = IntegerField(default=1)

    class Meta:
        table_name = "accounts"


class Prompt(BaseModel):
    id = AutoField()
    name = TextField()
    instructions = TextField()
    label_name = TextField()
    active = IntegerField(default=1)
    created_at = TextField(default=_now)
    action_archive = IntegerField(default=0)
    action_spam = IntegerField(default=0)
    action_trash = IntegerField(default=0)
    action_mark_read = IntegerField(default=0)
    sort_order = IntegerField(default=0)
    stop_processing = IntegerField(default=0)
    account_id = IntegerField(null=True)

    class Meta:
        table_name = "prompts"


class Setting(BaseModel):
    key = TextField(primary_key=True)
    value = TextField()

    class Meta:
        table_name = "settings"


class ProcessedEmail(BaseModel):
    id = AutoField()
    account_id = IntegerField()
    message_id = TextField()
    processed_at = TextField(null=True, default=_now)

    class Meta:
        table_name = "processed_emails"
        indexes = ((("account_id", "message_id"), True),)


class Log(BaseModel):
    id = AutoField()
    timestamp = TextField(default=_now)
    level = TextField()
    message = TextField()

    class Meta:
        table_name = "logs"


class CategorizationHistory(BaseModel):
    id = AutoField()
    timestamp = TextField(default=_now)
    account_id = IntegerField()
    account_email = TextField()
    message_id = TextField()
    subject = TextField(default="")
    sender = TextField(default="")
    prompt_id = IntegerField(null=True)
    prompt_name = TextField(null=True)
    label_name = TextField(null=True)
    actions = TextField(default="")
    llm_response = TextField(default="")

    class Meta:
        table_name = "categorization_history"


class AccountRetention(BaseModel):
    account_id = IntegerField(primary_key=True)
    global_days = IntegerField(null=True)

    class Meta:
        table_name = "account_retention"


class LabelRetention(BaseModel):
    id = AutoField()
    account_id = IntegerField()
    label_name = TextField()
    days = IntegerField()

    class Meta:
        table_name = "label_retention"
        indexes = ((("account_id", "label_name"), True),)


class LabelExemption(BaseModel):
    id = AutoField()
    account_id = IntegerField()
    label_name = TextField()

    class Meta:
        table_name = "label_exemptions"
        indexes = ((("account_id", "label_name"), True),)


class SchemaVersion(BaseModel):
    version = IntegerField(default=0)

    class Meta:
        table_name = "schema_version"


ALL_MODELS = [
    Account,
    Prompt,
    Setting,
    ProcessedEmail,
    Log,
    CategorizationHistory,
    AccountRetention,
    LabelRetention,
    LabelExemption,
    SchemaVersion,
]
