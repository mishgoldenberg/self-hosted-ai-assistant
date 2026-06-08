"""
log_store.py — Calorie and habit log (SQLite, stdlib only).

Rules enforced here (not just in the prompt):
  • No targets, deficits, macros, or diet advice — ever.
  • No evaluative commentary on amounts.
  • Recall returns raw entries only.

Public API
──────────
  log_calories(date_str, item, calories=None)   -> dict
  log_habit(date_str, habit)                    -> dict
  get_log(date_str)          -> {calories: [...], habits: [...], total_cal: int|None}
  get_log_range(start, end)  -> same shape, aggregated
  delete_entry(table, entry_id)                 -> bool
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "memory.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS calorie_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            date      TEXT    NOT NULL,
            item      TEXT    NOT NULL,
            calories  INTEGER,
            logged_at TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS habit_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            date      TEXT    NOT NULL,
            habit     TEXT    NOT NULL,
            logged_at TEXT    NOT NULL DEFAULT (datetime('now'))
        );
    """)
    conn.commit()


def log_calories(date_str: str, item: str, calories: int | None = None) -> dict:
    """Record a food/drink entry. calories is optional."""
    item = item.strip()
    if not item:
        return {"error": "Item description cannot be empty."}
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO calorie_log (date, item, calories) VALUES (?, ?, ?)",
            (date_str, item, calories),
        )
        return {"logged": True, "id": cur.lastrowid, "date": date_str,
                "item": item, "calories": calories}


def log_habit(date_str: str, habit: str) -> dict:
    """Record a habit entry."""
    habit = habit.strip()
    if not habit:
        return {"error": "Habit description cannot be empty."}
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO habit_log (date, habit) VALUES (?, ?)",
            (date_str, habit),
        )
        return {"logged": True, "id": cur.lastrowid, "date": date_str, "habit": habit}


def get_log(date_str: str) -> dict:
    """Return all calorie and habit entries for a single date."""
    with _conn() as conn:
        cal_rows = conn.execute(
            "SELECT id, item, calories FROM calorie_log WHERE date = ? ORDER BY id",
            (date_str,),
        ).fetchall()
        hab_rows = conn.execute(
            "SELECT id, habit FROM habit_log WHERE date = ? ORDER BY id",
            (date_str,),
        ).fetchall()

    calories = [dict(r) for r in cal_rows]
    total    = sum(r["calories"] for r in calories if r["calories"] is not None)
    return {
        "date":      date_str,
        "calories":  calories,
        "total_cal": total if calories else None,
        "habits":    [dict(r) for r in hab_rows],
    }


def get_log_range(start: str, end: str) -> dict:
    """Return entries for a date range (inclusive). Dates are YYYY-MM-DD strings."""
    with _conn() as conn:
        cal_rows = conn.execute(
            "SELECT id, date, item, calories FROM calorie_log "
            "WHERE date BETWEEN ? AND ? ORDER BY date, id",
            (start, end),
        ).fetchall()
        hab_rows = conn.execute(
            "SELECT id, date, habit FROM habit_log "
            "WHERE date BETWEEN ? AND ? ORDER BY date, id",
            (start, end),
        ).fetchall()

    calories = [dict(r) for r in cal_rows]
    total    = sum(r["calories"] for r in calories if r["calories"] is not None)
    return {
        "start":     start,
        "end":       end,
        "calories":  calories,
        "total_cal": total if calories else None,
        "habits":    [dict(r) for r in hab_rows],
    }


def delete_entry(table: str, entry_id: int) -> bool:
    """Delete a log entry by id. table must be 'calorie_log' or 'habit_log'."""
    if table not in ("calorie_log", "habit_log"):
        return False
    with _conn() as conn:
        cur = conn.execute(f"DELETE FROM {table} WHERE id = ?", (entry_id,))
        return cur.rowcount > 0
