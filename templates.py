"""
templates.py — Recurring task templates (SQLite, stdlib only).

Schema:
  templates   — id, name (unique), created
  template_tasks — id, template_id (FK), position, title

Public API
──────────
  save_template(name, tasks)           create or replace
  get_template(name) -> list[str]      task titles, ordered
  list_templates()   -> list[dict]     [{name, task_count, created}]
  delete_template(name)
  template_exists(name) -> bool
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "memory.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS templates (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            name    TEXT    NOT NULL UNIQUE COLLATE NOCASE,
            created TEXT    NOT NULL DEFAULT (date('now'))
        );
        CREATE TABLE IF NOT EXISTS template_tasks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            template_id INTEGER NOT NULL REFERENCES templates(id) ON DELETE CASCADE,
            position    INTEGER NOT NULL DEFAULT 0,
            title       TEXT    NOT NULL
        );
    """)
    conn.commit()


def save_template(name: str, tasks: list[str]) -> None:
    """Create or replace a template. tasks is an ordered list of task titles."""
    name = name.strip()
    if not name:
        raise ValueError("Template name cannot be empty.")
    tasks = [t.strip() for t in tasks if t.strip()]
    if not tasks:
        raise ValueError("Template must have at least one task.")
    with _conn() as conn:
        conn.execute("DELETE FROM templates WHERE name = ? COLLATE NOCASE", (name,))
        cur = conn.execute("INSERT INTO templates (name) VALUES (?)", (name,))
        tid = cur.lastrowid
        conn.executemany(
            "INSERT INTO template_tasks (template_id, position, title) VALUES (?, ?, ?)",
            [(tid, i, title) for i, title in enumerate(tasks)],
        )


def get_template(name: str) -> list[str]:
    """Return ordered task titles for the named template, or [] if not found."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT id FROM templates WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        if not row:
            return []
        rows = conn.execute(
            "SELECT title FROM template_tasks WHERE template_id = ? ORDER BY position",
            (row["id"],),
        ).fetchall()
        return [r["title"] for r in rows]


def list_templates() -> list[dict]:
    """Return all templates as [{name, task_count, created}]."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT t.name, t.created, COUNT(tt.id) AS task_count
            FROM templates t
            LEFT JOIN template_tasks tt ON tt.template_id = t.id
            GROUP BY t.id
            ORDER BY t.name COLLATE NOCASE
        """).fetchall()
        return [dict(r) for r in rows]


def delete_template(name: str) -> bool:
    """Delete a template. Returns True if it existed, False if not found."""
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM templates WHERE name = ? COLLATE NOCASE", (name,)
        )
        return cur.rowcount > 0


def template_exists(name: str) -> bool:
    with _conn() as conn:
        return conn.execute(
            "SELECT 1 FROM templates WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone() is not None
