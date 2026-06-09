"""
gmail_tools.py — Gmail read-only tools.

Public API
──────────
  summarize_unread(max_messages=10)  →  list[dict]
  search_email(query, max_results=10) →  list[dict]

Each returned dict:
  {id, thread_id, sender, subject, date, snippet, is_urgent}

"is_urgent" is a deterministic heuristic — never invented by the model.
"""

import base64
import re
from datetime import datetime, timezone
from email import message_from_bytes
from auth import get_gmail_service

# ── Lazy service init ─────────────────────────────────────────────────────────

_gmail_svc = None


def _get_gmail():
    global _gmail_svc
    if _gmail_svc is None:
        _gmail_svc = get_gmail_service()
    return _gmail_svc


# ── Urgency heuristic ─────────────────────────────────────────────────────────
# Deterministic keyword scan — never asks the model to judge urgency.

_URGENT_RE = re.compile(
    r'\b(urgent|asap|action required|immediate|deadline|expires|overdue|'
    r'payment due|invoice|reminder|confirm|verify|suspension|alert|'
    r'security|הודעה|דחוף|חשוב)\b',
    re.IGNORECASE,
)


def _is_urgent(subject: str, snippet: str) -> bool:
    return bool(_URGENT_RE.search(subject) or _URGENT_RE.search(snippet))


# ── Header extractor ──────────────────────────────────────────────────────────

def _extract_headers(headers: list[dict]) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in headers}


def _parse_date(date_str: str) -> str:
    """Return a compact human date like 'Jun 8' or 'Jun 8 2024'."""
    if not date_str:
        return ""
    try:
        # RFC 2822 — strip timezone name in parens first
        clean = re.sub(r'\s*\([^)]*\)', '', date_str).strip()
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(clean)
        today = datetime.now(timezone.utc).date()
        if dt.date().year == today.year:
            return dt.strftime("%-d %b").lstrip("0") if hasattr(dt, "strftime") else dt.strftime("%d %b")
        return dt.strftime("%d %b %Y")
    except Exception:
        return date_str[:16]


# ── Core fetcher ──────────────────────────────────────────────────────────────

def _fetch_messages(msg_ids: list[str]) -> list[dict]:
    """Fetch message metadata for a list of message ids."""
    svc = _get_gmail()
    results = []
    for mid in msg_ids:
        try:
            msg = svc.users().messages().get(
                userId="me", id=mid,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
        except Exception as exc:
            results.append({"id": mid, "error": str(exc)})
            continue

        hdrs    = _extract_headers(msg.get("payload", {}).get("headers", []))
        subject = hdrs.get("subject", "(no subject)")
        sender  = hdrs.get("from", "(unknown)")
        date    = _parse_date(hdrs.get("date", ""))
        snippet = msg.get("snippet", "")

        results.append({
            "id":        mid,
            "thread_id": msg.get("threadId", ""),
            "sender":    sender,
            "subject":   subject,
            "date":      date,
            "snippet":   snippet[:160],
            "is_urgent": _is_urgent(subject, snippet),
        })
    return results


# ── Public API ────────────────────────────────────────────────────────────────

def summarize_unread(max_messages: int = 10) -> list[dict]:
    """
    Return up to max_messages unread messages from the inbox.
    Results are ordered newest-first.
    Never fabricates — every field comes directly from the Gmail API.
    """
    svc = _get_gmail()
    resp = svc.users().messages().list(
        userId="me",
        labelIds=["INBOX", "UNREAD"],   # hard label filter — excludes Spam/Trash/All Mail
        maxResults=min(max_messages, 25),
    ).execute()
    ids = [m["id"] for m in resp.get("messages", [])]
    if not ids:
        return []
    return _fetch_messages(ids)


def search_email(query: str, max_results: int = 10) -> list[dict]:
    """
    Search Gmail with the given query string (same syntax as the Gmail search box).
    Returns up to max_results message summaries.
    """
    if not query or not query.strip():
        return [{"error": "Empty search query."}]
    svc = _get_gmail()
    resp = svc.users().messages().list(
        userId="me",
        q=query.strip(),
        maxResults=min(max_results, 25),
    ).execute()
    ids = [m["id"] for m in resp.get("messages", [])]
    if not ids:
        return []
    return _fetch_messages(ids)
