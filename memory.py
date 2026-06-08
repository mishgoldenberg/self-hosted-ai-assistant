"""
memory.py — Persistent memory store (SQLite, stdlib only).

Schema: id (auto), fact (text), category (tag), created (ISO date).
"""

import re
import sqlite3
from datetime import date
from pathlib import Path

DB_PATH = Path(__file__).parent / "memory.db"

# Patterns that look like credentials — never store these
_SECRET_RE = re.compile(
    r'(?i)(password|passwd|secret|token|api[_\s]?key|private[_\s]?key|bearer\s|'
    r'access[_\s]?key|credential)\s*[:=]\s*\S+'
    r'|(?:ghp_|xox[abp]-|sk-)[A-Za-z0-9_\-]{10,}'   # GitHub / Slack / OpenAI prefixes
    r'|[A-Za-z0-9+/]{40,}={0,2}(?:\s|$)'             # long base64-ish blobs
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            fact     TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            created  TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def looks_like_secret(text: str) -> bool:
    return bool(_SECRET_RE.search(text))


def add(fact: str, category: str = "general") -> dict:
    """Add a memory. Returns the new row dict, or {"error": ...} on refusal."""
    fact = fact.strip()
    category = category.strip() or "general"
    if looks_like_secret(fact):
        return {"error": "Refused: fact looks like a credential. Secrets are never stored."}
    if not fact:
        return {"error": "Fact text is empty."}
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO memories (fact, category, created) VALUES (?, ?, ?)",
            (fact, category, date.today().isoformat()),
        )
        row = conn.execute("SELECT * FROM memories WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)


def get_all() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM memories ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def forget(memory_id: int) -> dict:
    """Delete one memory by id. Returns {"deleted": row} or {"error": ...}."""
    with _conn() as conn:
        row = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        if row is None:
            return {"error": f"No memory with id={memory_id}."}
        conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        conn.commit()
        return {"deleted": dict(row)}


def clear_all() -> int:
    """Delete all memories. Returns the count deleted."""
    with _conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        conn.execute("DELETE FROM memories")
        conn.commit()
        return count


def search_relevant(query: str, limit: int = 6) -> list[dict]:
    """
    Return memories relevant to query (keyword match on fact + category).
    Falls back to most-recent `limit` entries when nothing matches.
    """
    all_mems = get_all()
    if not all_mems:
        return []

    words = {w for w in re.split(r'\W+', query.lower()) if len(w) > 2}
    if words:
        scored = []
        for m in all_mems:
            haystack = (m["fact"] + " " + m["category"]).lower()
            score = sum(1 for w in words if w in haystack)
            if score > 0:
                scored.append((score, m))
        scored.sort(key=lambda x: -x[0])
        if scored:
            return [m for _, m in scored[:limit]]

    # No keyword match — return most recent
    return all_mems[-limit:]


def format_for_prompt(memories: list[dict]) -> str:
    """Format a list of memory dicts as a compact prompt block."""
    lines = ["MEMORY FROM PREVIOUS SESSIONS (treat as established facts):"]
    for m in memories:
        lines.append(f"  • [{m['category']}] {m['fact']}")
    return "\n".join(lines)
