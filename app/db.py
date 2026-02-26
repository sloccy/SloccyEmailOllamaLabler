import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.path.join(os.getenv("DATA_DIR", "/data"), "labeler.db")


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
                UNIQUE(account_id, message_id)
            );

            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT DEFAULT (datetime('now')),
                level TEXT NOT NULL,
                message TEXT NOT NULL
            );

            INSERT OR IGNORE INTO settings (key, value) VALUES ('poll_interval', '300');
        """)
    _migrate()


def _migrate():
    """Safe migration: add columns that may not exist in older installs."""
    migrations = [
        "ALTER TABLE prompts ADD COLUMN action_archive INTEGER DEFAULT 0",
        "ALTER TABLE prompts ADD COLUMN action_spam INTEGER DEFAULT 0",
        "ALTER TABLE prompts ADD COLUMN action_move_to TEXT DEFAULT ''",  # kept for legacy DBs
        "ALTER TABLE prompts ADD COLUMN action_trash INTEGER DEFAULT 0",
        "ALTER TABLE prompts ADD COLUMN sort_order INTEGER DEFAULT 0",
        "ALTER TABLE prompts ADD COLUMN stop_processing INTEGER DEFAULT 0",
        "ALTER TABLE prompts ADD COLUMN account_id INTEGER DEFAULT NULL",
    ]
    with get_db() as conn:
        for sql in migrations:
            try:
                conn.execute(sql)
            except Exception:
                pass
    # Seed sort_order for existing rows
    with get_db() as conn:
        conn.execute("UPDATE prompts SET sort_order = id WHERE sort_order = 0")


# ---- Settings ----

def get_setting(key, default=None):
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value)
        )


# ---- Accounts ----

def list_accounts():
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM accounts ORDER BY added_at DESC"
        ).fetchall()]


def get_account(account_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return dict(row) if row else None


def upsert_account(email, credentials_json):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO accounts (email, credentials_json)
               VALUES (?, ?)
               ON CONFLICT(email) DO UPDATE SET credentials_json = excluded.credentials_json, active = 1""",
            (email, credentials_json),
        )


def update_account_credentials(account_id, credentials_json):
    with get_db() as conn:
        conn.execute(
            "UPDATE accounts SET credentials_json = ? WHERE id = ?",
            (credentials_json, account_id),
        )


def update_last_scan(account_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE accounts SET last_scan_at = datetime('now') WHERE id = ?", (account_id,)
        )


def delete_account(account_id):
    with get_db() as conn:
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        conn.execute("DELETE FROM processed_emails WHERE account_id = ?", (account_id,))


# ---- Prompts ----

def list_prompts(account_id=None):
    """
    Return prompts. If account_id is given, return prompts that either belong
    to that account or are global (account_id IS NULL). Otherwise return all prompts.
    """
    with get_db() as conn:
        if account_id is not None:
            rows = conn.execute(
                """SELECT * FROM prompts
                   WHERE (account_id = ? OR account_id IS NULL)
                   ORDER BY sort_order ASC, id ASC""",
                (account_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM prompts ORDER BY sort_order ASC, id ASC"
            ).fetchall()
    return [dict(r) for r in rows]


def create_prompt(name, instructions, label_name, action_archive=0, action_spam=0,
                  action_trash=0, stop_processing=0, account_id=None):
    with get_db() as conn:
        row = conn.execute("SELECT MAX(sort_order) as m FROM prompts").fetchone()
        next_order = (row["m"] or 0) + 1
        conn.execute(
            """INSERT INTO prompts
               (name, instructions, label_name, action_archive, action_spam,
                action_trash, sort_order, stop_processing, account_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, instructions, label_name, action_archive, action_spam,
             action_trash, next_order, stop_processing,
             account_id if account_id else None),
        )


def update_prompt(prompt_id, name, instructions, label_name, active,
                  action_archive=0, action_spam=0, action_trash=0,
                  stop_processing=0, account_id=None):
    with get_db() as conn:
        conn.execute(
            """UPDATE prompts SET name=?, instructions=?, label_name=?, active=?,
               action_archive=?, action_spam=?, action_trash=?,
               stop_processing=?, account_id=? WHERE id=?""",
            (name, instructions, label_name, active, action_archive, action_spam,
             action_trash, stop_processing,
             account_id if account_id else None, prompt_id),
        )


def reorder_prompts(ordered_ids: list):
    with get_db() as conn:
        for i, pid in enumerate(ordered_ids):
            conn.execute("UPDATE prompts SET sort_order=? WHERE id=?", (i, pid))


def delete_prompt(prompt_id):
    with get_db() as conn:
        conn.execute("DELETE FROM prompts WHERE id = ?", (prompt_id,))


# ---- Processed emails ----

def is_processed(account_id, message_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_emails WHERE account_id=? AND message_id=?",
            (account_id, message_id),
        ).fetchone()
    return row is not None


def mark_processed(account_id, message_id):
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO processed_emails (account_id, message_id) VALUES (?, ?)",
            (account_id, message_id),
        )


# ---- Logs ----

def add_log(level, message):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO logs (level, message) VALUES (?, ?)", (level.upper(), message)
        )
    with get_db() as conn:
        conn.execute(
            "DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT 500)"
        )


def get_logs(limit=100):
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()]
