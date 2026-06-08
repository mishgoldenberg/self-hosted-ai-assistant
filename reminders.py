"""
reminders.py — Persistent reminder store (SQLite).

Schema: id, chat_id, message, fire_at (ISO UTC), created_at, fired (0/1)

Public API
──────────
  add(chat_id, message, fire_at_iso)  -> int (reminder id)
  get_pending()                       -> list[dict]  (all unfired, ordered by fire_at)
  get_pending_for_chat(chat_id)       -> list[dict]
  mark_fired(reminder_id)
  delete(reminder_id, chat_id)        -> bool
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "memory.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    INTEGER NOT NULL,
            message    TEXT    NOT NULL,
            fire_at    TEXT    NOT NULL,
            created_at TEXT    NOT NULL DEFAULT (datetime('now')),
            fired      INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def add(chat_id: int, message: str, fire_at_iso: str) -> int:
    """
    Schedule a reminder. fire_at_iso must be a parseable ISO 8601 datetime.
    Returns the new reminder id.
    """
    # Validate / normalise the datetime
    fire_at_iso = _normalise_dt(fire_at_iso)
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO reminders (chat_id, message, fire_at) VALUES (?, ?, ?)",
            (chat_id, message.strip(), fire_at_iso),
        )
        return cur.lastrowid


def _normalise_dt(s: str) -> str:
    """Parse a variety of ISO-like strings and return YYYY-MM-DDTHH:MM:SS."""
    s = s.strip().replace(" ", "T")
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(s[:len(fmt) + 6], fmt)
            return dt.strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
    raise ValueError(f"Cannot parse datetime: {s!r}")


def get_pending() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM reminders WHERE fired = 0 ORDER BY fire_at"
        ).fetchall()
        return [dict(r) for r in rows]


def get_pending_for_chat(chat_id: int) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM reminders WHERE fired = 0 AND chat_id = ? ORDER BY fire_at",
            (chat_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_fired(reminder_id: int) -> None:
    with _conn() as conn:
        conn.execute("UPDATE reminders SET fired = 1 WHERE id = ?", (reminder_id,))


def delete(reminder_id: int, chat_id: int) -> bool:
    """Delete an unfired reminder. Returns True if deleted."""
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM reminders WHERE id = ? AND chat_id = ? AND fired = 0",
            (reminder_id, chat_id),
        )
        return cur.rowcount > 0
