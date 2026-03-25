import os
import sqlite3
import time
from contextlib import contextmanager

from app.config import LOG_RETENTION_DAYS, POLL_INTERVAL

DB_PATH = os.path.join(os.getenv("DATA_DIR", "/data"), "labeler.db")

_conn: sqlite3.Connection | None = None


def _get_connection() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


@contextmanager
def get_db():
    conn = _get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


@contextmanager
def get_db_readonly():
    yield _get_connection()


# ---- Generic query helpers ----


def _fetch_one(sql, params=()):
    with get_db_readonly() as conn:
        row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def _fetch_all(sql, params=()):
    with get_db_readonly() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _execute(sql, params=()):
    with get_db() as conn:
        conn.execute(sql, params)


# ---- Schema & Migrations ----


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                credentials_json TEXT NOT NULL,
                added_at TEXT DEFAULT (datetime('now')),
                last_scan_at TEXT,
                active INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS prompts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                instructions TEXT NOT NULL,
                label_name TEXT NOT NULL,
                active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now')),
                action_archive INTEGER DEFAULT 0,
                action_spam INTEGER DEFAULT 0,
                action_trash INTEGER DEFAULT 0,
                action_mark_read INTEGER DEFAULT 0,
                sort_order INTEGER DEFAULT 0,
                stop_processing INTEGER DEFAULT 0,
                account_id INTEGER DEFAULT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS processed_emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                message_id TEXT NOT NULL,
                processed_at TEXT DEFAULT (datetime('now')),
                UNIQUE(account_id, message_id)
            );

            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT DEFAULT (datetime('now')),
                level TEXT NOT NULL,
                message TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS categorization_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT DEFAULT (datetime('now')),
                account_id INTEGER NOT NULL,
                account_email TEXT NOT NULL,
                message_id TEXT NOT NULL,
                subject TEXT DEFAULT '',
                sender TEXT DEFAULT '',
                prompt_id INTEGER,
                prompt_name TEXT,
                label_name TEXT,
                actions TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS account_retention (
                account_id INTEGER PRIMARY KEY,
                global_days INTEGER
            );

            CREATE TABLE IF NOT EXISTS label_retention (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                label_name TEXT NOT NULL,
                days INTEGER NOT NULL,
                UNIQUE(account_id, label_name)
            );

            CREATE TABLE IF NOT EXISTS label_exemptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                label_name TEXT NOT NULL,
                UNIQUE(account_id, label_name)
            );

            CREATE INDEX IF NOT EXISTS idx_processed_emails_account_id ON processed_emails(account_id);
            CREATE INDEX IF NOT EXISTS idx_processed_emails_message_id ON processed_emails(message_id);
            CREATE INDEX IF NOT EXISTS idx_processed_emails_processed_at ON processed_emails(processed_at);
            CREATE INDEX IF NOT EXISTS idx_accounts_active ON accounts(active);
            CREATE INDEX IF NOT EXISTS idx_prompts_active ON prompts(active);
            CREATE INDEX IF NOT EXISTS idx_prompts_account_id ON prompts(account_id);
            CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp);
            CREATE INDEX IF NOT EXISTS idx_cat_history_account_id ON categorization_history(account_id);
            CREATE INDEX IF NOT EXISTS idx_cat_history_prompt_id ON categorization_history(prompt_id);
            CREATE INDEX IF NOT EXISTS idx_cat_history_timestamp ON categorization_history(timestamp);
        """)
    _execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('poll_interval', ?)", (str(POLL_INTERVAL),))
    _migrate()


def _migrate():
    """Safe migration: add columns/tables that may not exist in older installs."""
    migrations = [
        "ALTER TABLE prompts ADD COLUMN action_archive INTEGER DEFAULT 0",
        "ALTER TABLE prompts ADD COLUMN action_spam INTEGER DEFAULT 0",
        "ALTER TABLE prompts ADD COLUMN action_trash INTEGER DEFAULT 0",
        "ALTER TABLE prompts ADD COLUMN action_mark_read INTEGER DEFAULT 0",
        "ALTER TABLE prompts ADD COLUMN sort_order INTEGER DEFAULT 0",
        "ALTER TABLE prompts ADD COLUMN stop_processing INTEGER DEFAULT 0",
        "ALTER TABLE prompts ADD COLUMN account_id INTEGER DEFAULT NULL",
        "ALTER TABLE processed_emails ADD COLUMN processed_at TEXT",
        "CREATE INDEX IF NOT EXISTS idx_processed_emails_processed_at ON processed_emails(processed_at)",
        "CREATE TABLE IF NOT EXISTS account_retention (account_id INTEGER PRIMARY KEY, global_days INTEGER)",
        "CREATE TABLE IF NOT EXISTS label_retention (id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL, label_name TEXT NOT NULL, days INTEGER NOT NULL, UNIQUE(account_id, label_name))",
        "CREATE TABLE IF NOT EXISTS label_exemptions (id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL, label_name TEXT NOT NULL, UNIQUE(account_id, label_name))",
    ]
    with get_db() as conn:
        for sql in migrations:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # column/table already exists
        conn.execute("UPDATE prompts SET sort_order = id WHERE sort_order = 0")


# ---- Settings ----


def get_setting(key, default=None):
    row = _fetch_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set_setting(key, value):
    _execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))


# ---- Accounts ----


def list_accounts():
    return _fetch_all("SELECT * FROM accounts ORDER BY added_at DESC")


def list_accounts_safe():
    """List accounts without credentials_json — for UI display only."""
    return _fetch_all("SELECT id, email, added_at, last_scan_at, active FROM accounts ORDER BY added_at DESC")


def get_account(account_id):
    return _fetch_one("SELECT * FROM accounts WHERE id = ?", (account_id,))


def upsert_account(email, credentials_json):
    _execute(
        "INSERT INTO accounts (email, credentials_json) VALUES (?, ?)"
        " ON CONFLICT(email) DO UPDATE SET credentials_json = excluded.credentials_json, active = 1",
        (email, credentials_json),
    )


def update_account_credentials(account_id, credentials_json):
    _execute("UPDATE accounts SET credentials_json = ? WHERE id = ?", (credentials_json, account_id))


def update_last_scan(account_id):
    _execute("UPDATE accounts SET last_scan_at = datetime('now') WHERE id = ?", (account_id,))


def delete_account(account_id):
    with get_db() as conn:
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        conn.execute("DELETE FROM processed_emails WHERE account_id = ?", (account_id,))


def _toggle_active(table: str, row_id: int) -> int | None:
    with get_db() as conn:
        conn.execute(f"UPDATE {table} SET active = 1 - active WHERE id = ?", (row_id,))
        row = conn.execute(f"SELECT active FROM {table} WHERE id = ?", (row_id,)).fetchone()
    return row["active"] if row else None


def toggle_account(account_id):
    return _toggle_active("accounts", account_id)


# ---- Prompts ----


def list_prompts(account_id=None):
    """Return prompts. If account_id given, return prompts for that account plus global ones."""
    if account_id is not None:
        return _fetch_all(
            "SELECT * FROM prompts WHERE (account_id = ? OR account_id IS NULL) ORDER BY sort_order ASC, id ASC",
            (account_id,),
        )
    return _fetch_all("SELECT * FROM prompts ORDER BY sort_order ASC, id ASC")


def get_prompt(prompt_id):
    return _fetch_one("SELECT * FROM prompts WHERE id = ?", (prompt_id,))


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
    with get_db() as conn:
        row = conn.execute("SELECT MAX(sort_order) as m FROM prompts").fetchone()
        next_order = (row["m"] or 0) + 1
        conn.execute(
            "INSERT INTO prompts (name, instructions, label_name, action_archive, action_spam,"
            " action_trash, action_mark_read, sort_order, stop_processing, account_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                instructions,
                label_name,
                action_archive,
                action_spam,
                action_trash,
                action_mark_read,
                next_order,
                stop_processing,
                account_id if account_id else None,
            ),
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
    _execute(
        "UPDATE prompts SET name=?, instructions=?, label_name=?, active=?,"
        " action_archive=?, action_spam=?, action_trash=?, action_mark_read=?,"
        " stop_processing=?, account_id=? WHERE id=?",
        (
            name,
            instructions,
            label_name,
            active,
            action_archive,
            action_spam,
            action_trash,
            action_mark_read,
            stop_processing,
            account_id if account_id else None,
            prompt_id,
        ),
    )


def toggle_prompt(prompt_id) -> int | None:
    return _toggle_active("prompts", prompt_id)


def reorder_prompts(ordered_ids: list):
    with get_db() as conn:
        conn.executemany(
            "UPDATE prompts SET sort_order=? WHERE id=?",
            [(i + 1, pid) for i, pid in enumerate(ordered_ids)],
        )


def delete_prompt(prompt_id):
    _execute("DELETE FROM prompts WHERE id = ?", (prompt_id,))


# ---- Processed emails ----


def filter_unprocessed(account_id, message_ids: list) -> list:
    """Return the subset of message_ids that have NOT been processed yet."""
    if not message_ids:
        return []
    placeholders = ",".join("?" * len(message_ids))
    rows = _fetch_all(
        f"SELECT message_id FROM processed_emails WHERE account_id=? AND message_id IN ({placeholders})",
        [account_id] + list(message_ids),
    )
    already_processed = {r["message_id"] for r in rows}
    return [mid for mid in message_ids if mid not in already_processed]


def mark_processed(account_id, message_id):
    _execute("INSERT OR IGNORE INTO processed_emails (account_id, message_id) VALUES (?, ?)", (account_id, message_id))


def trim_processed_emails(lookback_hours):
    """Delete processed_emails entries older than 2x the lookback window."""
    _execute(
        "DELETE FROM processed_emails WHERE processed_at IS NOT NULL AND processed_at < datetime('now', ?)",
        (f"-{lookback_hours * 2} hours",),
    )


# ---- Logs ----


def add_log(level, message):
    _execute("INSERT INTO logs (level, message) VALUES (?, ?)", (level.upper(), message))


def trim_logs():
    retention_days = int(get_setting("log_retention_days", str(LOG_RETENTION_DAYS)))
    if retention_days > 0:
        cutoff = time.time() - (retention_days * 24 * 60 * 60)
        _execute("DELETE FROM logs WHERE timestamp < datetime(?, 'unixepoch')", (cutoff,))


def get_logs(limit=100):
    return _fetch_all("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,))


def get_logs_range(start, end):
    return _fetch_all("SELECT * FROM logs WHERE timestamp >= ? AND timestamp <= ? ORDER BY id ASC", (start, end))


# ---- Categorization History ----


def get_categorization_history(account_id=None, prompt_id=None, subject=None, sender=None, limit=200):
    wheres, params = [], []
    if account_id is not None:
        wheres.append("account_id = ?")
        params.append(account_id)
    if prompt_id is not None:
        wheres.append("prompt_id = ?")
        params.append(prompt_id)
    if subject:
        wheres.append("subject LIKE ?")
        params.append(f"%{subject}%")
    if sender:
        wheres.append("sender LIKE ?")
        params.append(f"%{sender}%")
    params.append(limit)
    sql = "SELECT * FROM categorization_history"
    if wheres:
        sql += " WHERE " + " AND ".join(wheres)
    sql += " ORDER BY id DESC LIMIT ?"
    return _fetch_all(sql, params)


# ---- Retention Rules ----


def get_retention(account_id):
    with get_db_readonly() as conn:
        row = conn.execute("SELECT global_days FROM account_retention WHERE account_id = ?", (account_id,)).fetchone()
        labels = conn.execute(
            "SELECT id, label_name, days FROM label_retention WHERE account_id = ? ORDER BY id ASC", (account_id,)
        ).fetchall()
        exemptions = conn.execute(
            "SELECT id, label_name FROM label_exemptions WHERE account_id = ? ORDER BY label_name ASC", (account_id,)
        ).fetchall()
    return {
        "global_days": row["global_days"] if row else None,
        "labels": [dict(r) for r in labels],
        "exemptions": [dict(r) for r in exemptions],
    }


def set_global_retention(account_id, days):
    _execute("INSERT OR REPLACE INTO account_retention (account_id, global_days) VALUES (?, ?)", (account_id, days))


def clear_global_retention(account_id):
    _execute("DELETE FROM account_retention WHERE account_id = ?", (account_id,))


def add_label_retention(account_id, label_name, days):
    _execute(
        "INSERT OR REPLACE INTO label_retention (account_id, label_name, days) VALUES (?, ?, ?)",
        (account_id, label_name, days),
    )


def delete_label_retention(rule_id):
    _execute("DELETE FROM label_retention WHERE id = ?", (rule_id,))


def add_label_exemption(account_id, label_name):
    _execute("INSERT OR IGNORE INTO label_exemptions (account_id, label_name) VALUES (?, ?)", (account_id, label_name))


def delete_label_exemption(exemption_id):
    _execute("DELETE FROM label_exemptions WHERE id = ?", (exemption_id,))


# ---- Import helpers ----


def create_account_placeholder(email):
    """Insert account with email only (no credentials). Returns account id."""
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO accounts (email, credentials_json, active) VALUES (?, '', 1)", (email,))
        row = conn.execute("SELECT id FROM accounts WHERE email = ?", (email,)).fetchone()
    return row["id"] if row else None


def prompt_exists(name, account_id):
    return _fetch_one("SELECT 1 FROM prompts WHERE name = ? AND account_id IS ?", (name, account_id)) is not None


def label_retention_exists(account_id, label_name):
    return (
        _fetch_one("SELECT 1 FROM label_retention WHERE account_id = ? AND label_name = ?", (account_id, label_name))
        is not None
    )


def has_global_retention(account_id):
    return _fetch_one("SELECT 1 FROM account_retention WHERE account_id = ?", (account_id,)) is not None
