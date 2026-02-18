import sqlite3
import os
from contextlib import contextmanager
from datetime import datetime

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
                created_at TEXT DEFAULT (datetime('now'))
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


# --- Settings ---

def get_setting(key: str, default=None):
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )


# --- Accounts ---

def list_accounts():
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM accounts ORDER BY added_at DESC").fetchall()]


def get_account(account_id: int):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return dict(row) if row else None


def upsert_account(email: str, credentials_json: str):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO accounts (email, credentials_json)
               VALUES (?, ?)
               ON CONFLICT(email) DO UPDATE SET credentials_json = excluded.credentials_json, active = 1""",
            (email, credentials_json),
        )


def update_account_credentials(account_id: int, credentials_json: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE accounts SET credentials_json = ? WHERE id = ?",
            (credentials_json, account_id),
        )


def update_last_scan(account_id: int):
    with get_db() as conn:
        conn.execute(
            "UPDATE accounts SET last_scan_at = datetime('now') WHERE id = ?",
            (account_id,),
        )


def delete_account(account_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        conn.execute("DELETE FROM processed_emails WHERE account_id = ?", (account_id,))


# --- Prompts ---

def list_prompts():
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM prompts ORDER BY created_at DESC").fetchall()]


def create_prompt(name: str, instructions: str, label_name: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO prompts (name, instructions, label_name) VALUES (?, ?, ?)",
            (name, instructions, label_name),
        )


def update_prompt(prompt_id: int, name: str, instructions: str, label_name: str, active: int):
    with get_db() as conn:
        conn.execute(
            "UPDATE prompts SET name=?, instructions=?, label_name=?, active=? WHERE id=?",
            (name, instructions, label_name, active, prompt_id),
        )


def delete_prompt(prompt_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM prompts WHERE id = ?", (prompt_id,))


# --- Processed emails ---

def is_processed(account_id: int, message_id: str) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_emails WHERE account_id=? AND message_id=?",
            (account_id, message_id),
        ).fetchone()
    return row is not None


def mark_processed(account_id: int, message_id: str):
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO processed_emails (account_id, message_id) VALUES (?, ?)",
            (account_id, message_id),
        )


# --- Logs ---

def add_log(level: str, message: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO logs (level, message) VALUES (?, ?)",
            (level.upper(), message),
        )
    # Trim to last 500 entries
    with get_db() as conn:
        conn.execute(
            "DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT 500)"
        )


def get_logs(limit: int = 100):
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()]
