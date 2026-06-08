"""
telegram_bot.py — Telegram interface for the personal assistant.

FREE-TEXT: any message → LLM agent → tools (unchanged)
GUIDED FLOWS (step-by-step, no LLM involved):
  /task  — create a task with prompts for each field
  /event — create a calendar event with prompts for each field
  /cancel — abort any active flow at any point

Both flows call the SAME create_task / create_calendar_event functions that
free-text creation uses — no duplicated logic.

Configuration (.env):
    TELEGRAM_BOT_TOKEN   — from @BotFather
    TELEGRAM_USER_ID     — your numeric Telegram user ID (allowlist)
"""

import asyncio
import datetime
import logging
import os
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from agent import run_agent, build_briefing, build_evening_briefing, build_digest, MODEL, _button_payloads
import memory as _memory
import weather as _weather
from tools import (
    get_task_lists as _get_task_lists,
    create_task as _create_task,
    create_calendar_event as _create_calendar_event,
    delete_calendar_event as _delete_calendar_event,
    complete_task_by_id as _complete_task_by_id,
    delete_task_by_id as _delete_task_by_id,
    _resolve_list_name,
    _get_services as _get_google_services,
)

# ── Configuration ─────────────────────────────────────────────────────────────

load_dotenv()

_token = os.environ.get("TELEGRAM_BOT_TOKEN")
_uid   = os.environ.get("TELEGRAM_USER_ID")

if not _token or not _uid:
    raise SystemExit(
        "Missing config. Create a .env file with:\n"
        "  TELEGRAM_BOT_TOKEN=<from BotFather>\n"
        "  TELEGRAM_USER_ID=<your numeric Telegram ID>"
    )

TELEGRAM_BOT_TOKEN = _token
ALLOWED_USER_ID    = int(_uid)

# ── Local timezone ────────────────────────────────────────────────────────────
_LOCAL_TZ: datetime.tzinfo = (
    datetime.datetime.now(datetime.timezone.utc).astimezone().tzinfo
)

# ── Briefing schedule ─────────────────────────────────────────────────────────
_BRIEFING_SCHEDULE = [
    (9,  0, "today"),
    (23, 0, "tomorrow"),
]
_DIGEST_HOUR   = 21   # evening digest time (local)
_DIGEST_MINUTE = 0

# ── Constants ─────────────────────────────────────────────────────────────────

MAX_HISTORY = 20
MAX_MSG_LEN = 4096

# ── Conversation states ───────────────────────────────────────────────────────
# Task flow (0–4)
TASK_NAME, TASK_DUE_DATE, TASK_DUE_DATETIME, TASK_LIST, TASK_CONFIRM = range(5)
# Event flow (10–13) — different range avoids collision if PTB merges state spaces
EVENT_NAME, EVENT_DATETIME, EVENT_REMINDER, EVENT_COLOR, EVENT_CONFIRM = range(10, 15)

# Keys for context.chat_data
_TD = "_task_data"    # dict accumulating task inputs
_ED = "_event_data"   # dict accumulating event inputs

# ── Shared state ──────────────────────────────────────────────────────────────

_histories: dict[int, list] = defaultdict(list)
_executor = ThreadPoolExecutor(max_workers=1)
_scheduler_task: asyncio.Task | None = None
_pending_memory_clear: set[int] = set()   # chat_ids awaiting /memory clear confirmation

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)


# ══════════════════════════════════════════════════════════════════════════════
# DATE / TIME PARSING HELPERS
# ══════════════════════════════════════════════════════════════════════════════

_SKIP_WORDS   = frozenset({"-", "−", "skip", "пропустить", "нет", "no", "none"})
_CANCEL_WORDS = frozenset({"cancel", "отмена", "/cancel"})
_YES_WORDS    = frozenset({"yes", "да", "y", "д", "ok", "ок", "✅", "✓", "yep", "sure"})
_NO_WORDS     = frozenset({"no", "нет", "n", "н", "nope", "cancel", "отмена"})

_DOW_MAP: dict[str, int] = {
    "monday": 0, "mon": 0, "понедельник": 0, "пн": 0,
    "tuesday": 1, "tue": 1, "вторник": 1, "вт": 1,
    "wednesday": 2, "wed": 2, "среда": 2, "среду": 2, "среды": 2, "ср": 2,
    "thursday": 3, "thu": 3, "четверг": 3, "чт": 3,
    "friday": 4, "fri": 4, "пятница": 4, "пятницу": 4, "пятницы": 4, "пт": 4,
    "saturday": 5, "sat": 5, "суббота": 5, "субботу": 5, "сб": 5,
    "sunday": 6, "sun": 6, "воскресенье": 6, "вс": 6,
}

_MONTH_MAP: dict[str, int] = {
    "january": 1,   "jan": 1,  "январь": 1,   "января": 1,
    "february": 2,  "feb": 2,  "февраль": 2,  "февраля": 2,
    "march": 3,     "mar": 3,  "март": 3,     "марта": 3,
    "april": 4,     "apr": 4,  "апрель": 4,   "апреля": 4,
    "may": 5,                  "май": 5,       "мая": 5,
    "june": 6,      "jun": 6,  "июнь": 6,     "июня": 6,
    "july": 7,      "jul": 7,  "июль": 7,     "июля": 7,
    "august": 8,    "aug": 8,  "август": 8,   "августа": 8,
    "september": 9, "sep": 9,  "сентябрь": 9, "сентября": 9,
    "october": 10,  "oct": 10, "октябрь": 10, "октября": 10,
    "november": 11, "nov": 11, "ноябрь": 11,  "ноября": 11,
    "december": 12, "dec": 12, "декабрь": 12, "декабря": 12,
}


def _parse_date(text: str) -> datetime.date | None:
    """
    Parse a date from user input. Returns a date object or None.
    Accepts: keywords (today/tomorrow/завтра), day names, DD.MM, DD.MM.YYYY,
             ISO YYYY-MM-DD, "D Month", "Month D".
    """
    t = text.strip().lower()
    today = datetime.date.today()

    if t in ("today", "сегодня"):
        return today
    if t in ("tomorrow", "завтра"):
        return today + datetime.timedelta(days=1)

    # "next <dow>"
    m = re.match(r'(?:next|следующ\w+)\s+(\w+)', t)
    if m:
        key = m.group(1)
        if key in _DOW_MAP:
            delta = (_DOW_MAP[key] - today.weekday() + 7) % 7 or 7
            return today + datetime.timedelta(days=delta)

    # bare day name → next occurrence
    if t in _DOW_MAP:
        delta = (_DOW_MAP[t] - today.weekday()) % 7 or 7
        return today + datetime.timedelta(days=delta)

    # "D Month" or "Month D" (e.g. "8 June", "June 8", "8 июня")
    m = re.match(r'(\d{1,2})\s+(\w+)$', t)
    if m and m.group(2) in _MONTH_MAP:
        try:
            return datetime.date(today.year, _MONTH_MAP[m.group(2)], int(m.group(1)))
        except ValueError:
            pass
    m = re.match(r'(\w+)\s+(\d{1,2})$', t)
    if m and m.group(1) in _MONTH_MAP:
        try:
            return datetime.date(today.year, _MONTH_MAP[m.group(1)], int(m.group(2)))
        except ValueError:
            pass

    # DD.MM[.YYYY]
    m = re.fullmatch(r'(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?', t)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        if year < 100:
            year += 2000
        try:
            return datetime.date(year, month, day)
        except ValueError:
            pass

    # ISO YYYY-MM-DD
    try:
        return datetime.date.fromisoformat(t)
    except ValueError:
        pass

    return None


def _parse_time_str(text: str) -> tuple[int, int] | None:
    """Parse a time string. Returns (hour, minute) or None."""
    t = text.strip().lower()

    # HH:MM
    m = re.fullmatch(r'(\d{1,2}):(\d{2})', t)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mn <= 59:
            return h, mn

    # Ham or H:MMam/pm
    m = re.fullmatch(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)', t)
    if m:
        h  = int(m.group(1))
        mn = int(m.group(2)) if m.group(2) else 0
        if m.group(3) == "pm" and h != 12:
            h += 12
        elif m.group(3) == "am" and h == 12:
            h = 0
        if 0 <= h <= 23 and 0 <= mn <= 59:
            return h, mn

    return None


def _parse_datetime_str(text: str) -> datetime.datetime | None:
    """
    Parse a datetime from user input. Returns a naive datetime or None.
    Handles: "tomorrow 15:00", "Friday 9am", "8.6 14:00", "2026-06-08T15:00",
             "tomorrow" (00:00), bare "15:00" (assumes today or tomorrow).
    """
    t = text.strip()

    # ISO datetime (with or without T separator)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.datetime.strptime(t, fmt)
        except ValueError:
            pass

    # Find the time component anywhere in the string
    time_m = re.search(r'\b(\d{1,2}:\d{2}|\d{1,2}\s*(?:am|pm))\b', t, re.IGNORECASE)
    if time_m:
        time_part = _parse_time_str(time_m.group(1))
        if time_part:
            h, mn = time_part
            date_str = (t[:time_m.start()] + t[time_m.end():]).strip()
            if date_str:
                d = _parse_date(date_str)
                if d:
                    return datetime.datetime.combine(d, datetime.time(h, mn))
            else:
                # Bare time — pick today if in future, else tomorrow
                now = datetime.datetime.now()
                candidate = datetime.datetime.combine(now.date(), datetime.time(h, mn))
                if candidate <= now:
                    candidate += datetime.timedelta(days=1)
                return candidate

    # No time found — try pure date → midnight
    d = _parse_date(t)
    if d:
        return datetime.datetime.combine(d, datetime.time(0, 0))

    return None


def _parse_event_times(text: str) -> tuple[datetime.datetime | None, datetime.datetime | None]:
    """
    Parse event start and end from user input.
    Handles:
      "tomorrow 14:00"               → start=14:00, end=15:00 (+1 hr default)
      "tomorrow 14:00-15:00"         → start=14:00, end=15:00
      "tomorrow 14:00 to 15:30"      → start=14:00, end=15:30
      "tomorrow 14:00 for 2 hours"   → start=14:00, end=16:00
      "tomorrow 14:00 for 90 min"    → start=14:00, end=15:30
    """
    t = text.strip()

    # "for N hours/minutes" suffix
    m = re.search(r'\bfor\s+(\d+(?:\.\d+)?)\s*(hour|hr|h|minute|min|m)s?\b', t, re.IGNORECASE)
    if m:
        amount = float(m.group(1))
        unit   = m.group(2).lower()
        start  = _parse_datetime_str(t[:m.start()].strip())
        if start:
            delta = (datetime.timedelta(hours=amount)
                     if unit in ("hour", "hr", "h")
                     else datetime.timedelta(minutes=amount))
            return start, start + delta

    # "HH:MM-HH:MM" or "HH:MM to HH:MM" range
    m = re.search(r'(\d{1,2}:\d{2})\s*(?:-|–|to)\s*(\d{1,2}:\d{2})', t, re.IGNORECASE)
    if m:
        st = _parse_time_str(m.group(1))
        et = _parse_time_str(m.group(2))
        date_str = (t[:m.start()] + t[m.end():]).strip()
        d = _parse_date(date_str) if date_str else datetime.date.today()
        if st and et and d:
            start = datetime.datetime.combine(d, datetime.time(*st))
            end   = datetime.datetime.combine(d, datetime.time(*et))
            return start, end

    # Single datetime → end = start + 1 hour
    start = _parse_datetime_str(t)
    if start:
        return start, start + datetime.timedelta(hours=1)

    return None, None


def _parse_reminder_minutes(text: str) -> tuple[int | None, bool]:
    """
    Parse reminder specification.
    Returns (minutes, valid).
      minutes=None  → skip / use calendar default
      minutes=int   → specific offset
      valid=False   → couldn't parse (caller should re-ask)
    """
    t = text.strip().lower()

    if t in _SKIP_WORDS or t in ("default", "по умолчанию"):
        return None, True  # use calendar default

    if t in ("at the event", "at event time", "at start", "0", "at time", "сейчас"):
        return 0, True

    # N minutes
    m = re.search(r'(\d+)\s*(?:minute|min|мин)', t)
    if m:
        return int(m.group(1)), True

    # N hours
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:hour|hr|ч(?:ас)?)', t)
    if m:
        return round(float(m.group(1)) * 60), True

    # N days
    m = re.search(r'(\d+)\s*(?:day|д(?:ень|ня|ней)?)', t)
    if m:
        return int(m.group(1)) * 1440, True

    # bare number → assume minutes
    m = re.fullmatch(r'(\d+)', t)
    if m:
        return int(m.group(1)), True

    return None, False  # invalid


# ── Display helpers ───────────────────────────────────────────────────────────

def _fmt_date(d: datetime.date) -> str:
    return f"{d.strftime('%A, %B')} {d.day}"

def _fmt_dt(dt: datetime.datetime) -> str:
    return f"{dt.strftime('%A, %B')} {dt.day} at {dt.strftime('%H:%M')}"

def _fmt_reminder(minutes: int | None) -> str:
    if minutes is None:
        return "calendar default"
    if minutes == 0:
        return "at event time"
    if minutes % 1440 == 0:
        n = minutes // 1440
        return f"{n} day{'s' if n != 1 else ''} before"
    if minutes % 60 == 0:
        n = minutes // 60
        return f"{n} hour{'s' if n != 1 else ''} before"
    return f"{minutes} minute{'s' if minutes != 1 else ''} before"


# ══════════════════════════════════════════════════════════════════════════════
# CANCEL / SHARED HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def _cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """End any active guided flow gracefully."""
    context.chat_data.pop(_TD, None)
    context.chat_data.pop(_ED, None)
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


async def _cancel_standalone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles /cancel when no ConversationHandler flow is active."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    if context.user_data.get("tmpl_state") == "tmpl_await_tasks":
        context.user_data.pop("tmpl_state", None)
        context.user_data.pop("tmpl_name", None)
        context.user_data.pop("tmpl_tasks", None)
        await update.message.reply_text("Template creation cancelled.")
        return
    await update.message.reply_text("No active flow to cancel.")


def _is_cancel(text: str) -> bool:
    return text.strip().lower() in _CANCEL_WORDS

def _is_skip(text: str) -> bool:
    return text.strip().lower() in _SKIP_WORDS


# ══════════════════════════════════════════════════════════════════════════════
# TASK FLOW
# ══════════════════════════════════════════════════════════════════════════════

async def _task_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ALLOWED_USER_ID:
        return ConversationHandler.END
    # Clear any stale event flow data
    context.chat_data.pop(_ED, None)

    # Fetch task lists upfront (needed for the list-selection step)
    loop = asyncio.get_running_loop()
    try:
        lists = await loop.run_in_executor(_executor, _get_task_lists)
    except Exception:
        lists = []

    context.chat_data[_TD] = {"lists": lists}

    await update.message.reply_text(
        "📝 <b>New Task — Step 1/5</b>\n\n"
        "Task name?",
        parse_mode="HTML",
    )
    return TASK_NAME


async def _task_get_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if _is_cancel(text):
        return await _cancel_flow(update, context)

    if not text:
        await update.message.reply_text("Please enter a name for the task.")
        return TASK_NAME

    context.chat_data[_TD]["name"] = text

    await update.message.reply_text(
        "📅 <b>Step 2/5 — Due date</b> (date only)\n\n"
        "Examples: <code>tomorrow</code>  <code>Friday</code>  <code>8.6</code>  <code>2026-06-12</code>\n"
        "Reply <code>-</code> to skip.",
        parse_mode="HTML",
    )
    return TASK_DUE_DATE


async def _task_get_due_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if _is_cancel(text):
        return await _cancel_flow(update, context)

    if _is_skip(text):
        context.chat_data[_TD]["due_date"] = None
    else:
        d = _parse_date(text)
        if d is None:
            await update.message.reply_text(
                "⚠️ Couldn't understand that date.\n"
                "Try: <code>tomorrow</code>, <code>Friday</code>, <code>8.6</code>, or <code>-</code> to skip.",
                parse_mode="HTML",
            )
            return TASK_DUE_DATE
        context.chat_data[_TD]["due_date"] = d.isoformat()

    await update.message.reply_text(
        "🕐 <b>Step 3/5 — Date and time</b> (for Calendar visibility)\n\n"
        "Examples: <code>tomorrow 15:00</code>  <code>Friday 9am</code>  <code>8.6 14:30</code>\n"
        "⚠️ Note: the Tasks API can't store a time — setting a time creates a Calendar event "
        "at that moment alongside the task.\n"
        "Reply <code>-</code> to skip.",
        parse_mode="HTML",
    )
    return TASK_DUE_DATETIME


async def _task_get_due_datetime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if _is_cancel(text):
        return await _cancel_flow(update, context)

    if _is_skip(text):
        context.chat_data[_TD]["due_datetime"] = None
    else:
        dt = _parse_datetime_str(text)
        if dt is None:
            await update.message.reply_text(
                "⚠️ Couldn't understand that date/time.\n"
                "Try: <code>tomorrow 15:00</code>, <code>Friday 9am</code>, or <code>-</code> to skip.",
                parse_mode="HTML",
            )
            return TASK_DUE_DATETIME
        context.chat_data[_TD]["due_datetime"] = dt.strftime("%Y-%m-%dT%H:%M:%S")

    lists = context.chat_data[_TD].get("lists", [])
    if lists:
        list_lines = "\n".join(
            f"  {i+1}. {tl['title']}{' ← default' if i == 0 else ''}"
            for i, tl in enumerate(lists)
        )
    else:
        list_lines = "  (could not fetch lists)"

    await update.message.reply_text(
        f"📋 <b>Step 4/5 — List</b>\n\n"
        f"{list_lines}\n\n"
        f"Reply with a number, list name, or <code>-</code> for the default list.",
        parse_mode="HTML",
    )
    return TASK_LIST


async def _task_get_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if _is_cancel(text):
        return await _cancel_flow(update, context)

    lists = context.chat_data[_TD].get("lists", [])

    if _is_skip(text):
        # Use default list
        list_id    = lists[0]["id"]    if lists else None
        list_title = lists[0]["title"] if lists else "primary"
    elif text.isdigit():
        idx = int(text) - 1
        if 0 <= idx < len(lists):
            list_id, list_title = lists[idx]["id"], lists[idx]["title"]
        else:
            await update.message.reply_text(
                f"⚠️ Invalid number. Choose 1–{len(lists)} or a list name."
            )
            return TASK_LIST
    else:
        loop = asyncio.get_running_loop()
        try:
            list_id, list_title = await loop.run_in_executor(
                _executor, lambda: _resolve_list_name(text)
            )
        except ValueError as exc:
            await update.message.reply_text(f"⚠️ {exc}")
            return TASK_LIST

    context.chat_data[_TD]["list_id"]    = list_id
    context.chat_data[_TD]["list_title"] = list_title

    # Build confirmation summary
    data         = context.chat_data[_TD]
    name         = data["name"]
    due_date     = data.get("due_date")
    due_datetime = data.get("due_datetime")

    if due_date:
        d = datetime.date.fromisoformat(due_date)
        due_line = f"  Due date:    {_fmt_date(d)}"
    else:
        due_line = "  Due date:    — (none)"

    if due_datetime:
        dt = datetime.datetime.fromisoformat(due_datetime)
        dt_line = f"  Date/time:   {_fmt_dt(dt)}  (+ Calendar event)"
    else:
        dt_line = "  Date/time:   — (no Calendar event)"

    summary = (
        f"📋 <b>Step 5/5 — Confirm</b>\n\n"
        f"<b>Task to create:</b>\n"
        f"  Name:        {name}\n"
        f"{due_line}\n"
        f"{dt_line}\n"
        f"  List:        {list_title}\n\n"
        f"Create this task? Reply <b>yes</b> or <b>no</b>."
    )
    await update.message.reply_text(summary, parse_mode="HTML")
    return TASK_CONFIRM


async def _task_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().lower()

    if text in _NO_WORDS or _is_cancel(text):
        return await _cancel_flow(update, context)

    if text not in _YES_WORDS:
        await update.message.reply_text("Please reply <b>yes</b> or <b>no</b>.", parse_mode="HTML")
        return TASK_CONFIRM

    data         = context.chat_data[_TD]
    name         = data["name"]
    due_date     = data.get("due_date")
    due_datetime = data.get("due_datetime")
    list_id      = data.get("list_id")
    list_title   = data.get("list_title")

    await update.message.reply_text("Creating task…")

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            _executor,
            lambda: _create_task(
                title=name,
                due_date=due_date if not due_datetime else None,
                due_datetime=due_datetime,
                list_name=list_title,
            ),
        )
    except Exception as exc:
        log.exception("create_task failed in guided flow")
        await update.message.reply_text(f"⚠️ Failed to create task: {exc}")
        context.chat_data.pop(_TD, None)
        return ConversationHandler.END

    # Build honest confirmation
    due_disp = result.get("due_date") or "—"
    reply_lines = [
        f"✅ <b>Task created</b>",
        f"  Name:   {result.get('title')}",
        f"  Due:    {due_disp}",
        f"  List:   {result.get('list')}",
    ]
    cal = result.get("calendar_event")
    if cal:
        reply_lines.append(
            f"  🗓 Calendar event also created at {cal.get('start', '?')[:16].replace('T', ' ')}"
        )

    context.chat_data.pop(_TD, None)
    await update.message.reply_text("\n".join(reply_lines), parse_mode="HTML")
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# EVENT FLOW
# ══════════════════════════════════════════════════════════════════════════════

async def _event_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ALLOWED_USER_ID:
        return ConversationHandler.END
    # Clear any stale task flow data
    context.chat_data.pop(_TD, None)

    context.chat_data[_ED] = {}
    await update.message.reply_text(
        "📅 <b>New Event — Step 1/4</b>\n\n"
        "Event name?",
        parse_mode="HTML",
    )
    return EVENT_NAME


async def _event_get_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if _is_cancel(text):
        return await _cancel_flow(update, context)

    if not text:
        await update.message.reply_text("Please enter a name for the event.")
        return EVENT_NAME

    context.chat_data[_ED]["name"] = text

    await update.message.reply_text(
        "🕐 <b>Step 2/4 — Date and time</b>\n\n"
        "Examples:\n"
        "  <code>tomorrow 14:00</code>  → 1-hour event\n"
        "  <code>Friday 14:00-15:30</code>  → explicit end\n"
        "  <code>tomorrow 14:00 for 2 hours</code>\n"
        "  <code>tomorrow 14:00 for 90 min</code>",
        parse_mode="HTML",
    )
    return EVENT_DATETIME


async def _event_get_datetime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if _is_cancel(text):
        return await _cancel_flow(update, context)

    start, end = _parse_event_times(text)
    if start is None:
        await update.message.reply_text(
            "⚠️ Couldn't parse that.\n"
            "Try: <code>tomorrow 14:00</code>, <code>Friday 14:00-15:00</code>, "
            "or <code>tomorrow 14:00 for 2 hours</code>.",
            parse_mode="HTML",
        )
        return EVENT_DATETIME

    context.chat_data[_ED]["start"] = start.strftime("%Y-%m-%dT%H:%M:%S")
    context.chat_data[_ED]["end"]   = end.strftime("%Y-%m-%dT%H:%M:%S")

    await update.message.reply_text(
        "🔔 <b>Step 3/4 — Reminder</b>\n\n"
        "How long before the event?\n"
        "Examples: <code>30 min</code>  <code>1 hour</code>  <code>1 day</code>  <code>10</code> (minutes)\n"
        "Reply <code>-</code> to use your calendar's default reminder.",
        parse_mode="HTML",
    )
    return EVENT_REMINDER


async def _event_get_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if _is_cancel(text):
        return await _cancel_flow(update, context)

    minutes, valid = _parse_reminder_minutes(text)
    if not valid:
        await update.message.reply_text(
            "⚠️ Couldn't understand that.\n"
            "Try: <code>30 min</code>, <code>1 hour</code>, <code>1 day</code>, or <code>-</code> to skip.",
            parse_mode="HTML",
        )
        return EVENT_REMINDER

    context.chat_data[_ED]["reminder_minutes"] = minutes

    from tools import CALENDAR_COLORS
    color_names = ", ".join(name for _, (name, _hex) in CALENDAR_COLORS.items())
    await update.message.reply_text(
        f"📅 <b>Step 4/5 — Color</b>\n\n"
        f"Choose an event color, or send <code>-</code> to skip.\n"
        f"Options: <i>{color_names}</i>\n"
        f"Or plain words: red, blue, green, yellow, orange, purple, pink, teal, gray",
        parse_mode="HTML",
    )
    return EVENT_COLOR


async def _event_get_color(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from html import escape as _e
    from tools import resolve_color, color_options_text, CALENDAR_COLORS

    text = update.message.text.strip()
    if _is_cancel(text):
        return await _cancel_flow(update, context)

    if _is_skip(text):
        context.chat_data[_ED]["color"] = None
        context.chat_data[_ED]["color_name"] = None
    else:
        resolved = resolve_color(text)
        if resolved is None:
            await update.message.reply_text(
                f"⚠️ Unknown color <b>{_e(text)}</b>.\n{_e(color_options_text())}\n\n"
                f"Or send <code>-</code> to skip.",
                parse_mode="HTML",
            )
            return EVENT_COLOR
        color_id, color_name = resolved
        context.chat_data[_ED]["color"]      = color_id
        context.chat_data[_ED]["color_name"] = color_name

    # Show confirmation
    data       = context.chat_data[_ED]
    name       = data["name"]
    start      = datetime.datetime.fromisoformat(data["start"])
    end        = datetime.datetime.fromisoformat(data["end"])
    rem        = _fmt_reminder(data.get("reminder_minutes"))
    color_line = data.get("color_name") or "—"

    summary = (
        f"📅 <b>Step 5/5 — Confirm</b>\n\n"
        f"<b>Event to create:</b>\n"
        f"  Name:     {_e(name)}\n"
        f"  Start:    {_fmt_dt(start)}\n"
        f"  End:      {_fmt_dt(end)}\n"
        f"  Reminder: {rem}\n"
        f"  Color:    {color_line}\n\n"
        f"Create this event? Reply <b>yes</b> or <b>no</b>."
    )
    await update.message.reply_text(summary, parse_mode="HTML")
    return EVENT_CONFIRM


async def _event_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from html import escape as _e

    text = update.message.text.strip().lower()

    if text in _NO_WORDS or _is_cancel(text):
        return await _cancel_flow(update, context)

    if text not in _YES_WORDS:
        await update.message.reply_text("Please reply <b>yes</b> or <b>no</b>.", parse_mode="HTML")
        return EVENT_CONFIRM

    data             = context.chat_data[_ED]
    name             = data["name"]
    start            = data["start"]
    end              = data["end"]
    reminder_minutes = data.get("reminder_minutes")
    color_id         = data.get("color")      # colorId string "1".."11" or None
    color_name       = data.get("color_name") # Google name or None

    await update.message.reply_text("Creating event…")

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            _executor,
            lambda: _create_calendar_event(
                title=name,
                start=start,
                end=end,
                reminder_minutes=reminder_minutes,
                color=color_name,   # pass Google name; resolve_color inside handles it
            ),
        )
    except Exception as exc:
        log.exception("create_calendar_event failed in guided flow")
        await update.message.reply_text(f"⚠️ Failed to create event: {exc}")
        context.chat_data.pop(_ED, None)
        return ConversationHandler.END

    if result.get("error"):
        await update.message.reply_text(f"⚠️ {_e(result['error'])}", parse_mode="HTML")
        context.chat_data.pop(_ED, None)
        return ConversationHandler.END

    stored_rem   = result.get("reminder_minutes")
    stored_color = result.get("color_name") or "—"
    reply_lines  = [
        "✅ <b>Event created</b>",
        f"  Name:     {_e(result.get('summary', ''))}",
        f"  Start:    {(result.get('start') or '')[:16].replace('T', ' ')}",
        f"  End:      {(result.get('end') or '')[:16].replace('T', ' ')}",
        f"  Reminder: {_fmt_reminder(stored_rem)}",
        f"  Color:    {stored_color}",
    ]

    context.chat_data.pop(_ED, None)
    await update.message.reply_text("\n".join(reply_lines), parse_mode="HTML")
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# CONVERSATION HANDLER INSTANCES
# ══════════════════════════════════════════════════════════════════════════════

_flow_cancel_filters = (
    filters.Regex(re.compile(r'^(cancel|отмена)$', re.IGNORECASE))
)

task_conv = ConversationHandler(
    entry_points=[CommandHandler("task", _task_start)],
    states={
        TASK_NAME:         [MessageHandler(filters.TEXT & ~filters.COMMAND, _task_get_name)],
        TASK_DUE_DATE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, _task_get_due_date)],
        TASK_DUE_DATETIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, _task_get_due_datetime)],
        TASK_LIST:         [MessageHandler(filters.TEXT & ~filters.COMMAND, _task_get_list)],
        TASK_CONFIRM:      [MessageHandler(filters.TEXT & ~filters.COMMAND, _task_confirm)],
    },
    fallbacks=[
        CommandHandler("cancel", _cancel_flow),
        MessageHandler(_flow_cancel_filters, _cancel_flow),
    ],
    conversation_timeout=600,   # auto-end after 10 min of silence
    name="task_flow",
    per_message=False,
)

event_conv = ConversationHandler(
    entry_points=[CommandHandler("event", _event_start)],
    states={
        EVENT_NAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, _event_get_name)],
        EVENT_DATETIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, _event_get_datetime)],
        EVENT_REMINDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, _event_get_reminder)],
        EVENT_COLOR:    [MessageHandler(filters.TEXT & ~filters.COMMAND, _event_get_color)],
        EVENT_CONFIRM:  [MessageHandler(filters.TEXT & ~filters.COMMAND, _event_confirm)],
    },
    fallbacks=[
        CommandHandler("cancel", _cancel_flow),
        MessageHandler(_flow_cancel_filters, _cancel_flow),
    ],
    conversation_timeout=600,
    name="event_flow",
    per_message=False,
)


# ══════════════════════════════════════════════════════════════════════════════
# FREE-TEXT HELPERS (unchanged from original)
# ══════════════════════════════════════════════════════════════════════════════

def _split_message(text: str) -> list[str]:
    if len(text) <= MAX_MSG_LEN:
        return [text]
    parts = []
    while text:
        if len(text) <= MAX_MSG_LEN:
            parts.append(text); break
        cut = text.rfind("\n", 0, MAX_MSG_LEN)
        if cut <= 0:
            cut = MAX_MSG_LEN
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts


async def _typing_loop(chat_id: int, bot, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            continue
        break


# ── Briefing helpers ──────────────────────────────────────────────────────────

def _briefing_header(date_keyword: str) -> str:
    today = datetime.date.today()
    if date_keyword == "today":
        day, emoji, word = today, "🌅", "Today"
    else:
        day, emoji, word = today + datetime.timedelta(days=1), "🌙", "Tomorrow"
    return f"{emoji} {word} — {day.strftime('%A, %B')} {day.day}"


async def _send_briefing(bot, date_keyword: str,
                         chat_id: int | None = None) -> None:
    """Morning briefing (09:00 / /briefing today)."""
    target = chat_id if chat_id is not None else ALLOWED_USER_ID
    header = _briefing_header(date_keyword)
    log.info("Sending morning briefing: %s → chat %d", header, target)
    try:
        loop = asyncio.get_running_loop()
        body = await loop.run_in_executor(
            _executor,
            lambda: build_briefing(date_keyword, lang="en", chat_id=target),
        )
        text = f"{header}\n{'─' * 30}\n\n{body}"
        for chunk in _split_message(text):
            await bot.send_message(chat_id=target, text=chunk)
        log.info("Morning briefing sent: %s", header)
    except Exception as exc:
        log.exception("Morning briefing failed: %s", header)
        try:
            await bot.send_message(
                chat_id=target,
                text=f"⚠️ Briefing failed ({header}):\n{type(exc).__name__}: {exc}",
            )
        except Exception:
            log.exception("Could not deliver briefing failure notice to chat %d", target)


async def _send_evening_briefing(bot, chat_id: int | None = None) -> None:
    """Evening briefing (23:00 / /briefing tomorrow) — accomplishments + tomorrow preview."""
    target = chat_id if chat_id is not None else ALLOWED_USER_ID
    log.info("Sending evening briefing → chat %d", target)
    try:
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(
            _executor,
            lambda: build_evening_briefing(chat_id=target),
        )
        for chunk in _split_message(text):
            await bot.send_message(chat_id=target, text=chunk)
        log.info("Evening briefing sent → chat %d", target)
    except Exception as exc:
        log.exception("Evening briefing failed")
        try:
            await bot.send_message(
                chat_id=target,
                text=f"⚠️ Evening briefing failed:\n{type(exc).__name__}: {exc}",
            )
        except Exception:
            log.exception("Could not deliver evening briefing failure notice to chat %d", target)


# ── Background scheduler loop ─────────────────────────────────────────────────

async def _fire_due_reminders(bot) -> None:
    """Send any reminders whose fire_at has passed and mark them fired."""
    import reminders as _rem
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    pending = _rem.get_pending()
    for r in pending:
        if r["fire_at"] <= now_iso:
            try:
                await bot.send_message(chat_id=r["chat_id"], text=f"⏰ {r['message']}")
                _rem.mark_fired(r["id"])
                log.info("Reminder %d fired → chat %d", r["id"], r["chat_id"])
            except Exception as exc:
                log.warning("Failed to send reminder %d: %s", r["id"], exc)


async def _send_digest(bot) -> None:
    """On-demand /digest command handler — today's accomplishments only."""
    loop = asyncio.get_running_loop()
    try:
        text = await loop.run_in_executor(_executor, build_digest)
    except Exception as exc:
        log.exception("build_digest failed: %s", exc)
        return
    for chunk in _split_message(text):
        await bot.send_message(chat_id=ALLOWED_USER_ID, text=chunk)


async def _scheduler_loop(bot) -> None:
    while True:
        now = datetime.datetime.now(tz=_LOCAL_TZ)

        # Scheduled fire times: 09:00 morning, 23:00 evening, 21:00 digest
        candidates: list[tuple[datetime.datetime, str]] = []

        # 09:00 — morning briefing (today)
        morning_t = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if morning_t <= now:
            morning_t += datetime.timedelta(days=1)
        candidates.append((morning_t, "briefing:today"))

        # 23:00 — evening briefing (accomplishments + tomorrow)
        evening_t = now.replace(hour=23, minute=0, second=0, microsecond=0)
        if evening_t <= now:
            evening_t += datetime.timedelta(days=1)
        candidates.append((evening_t, "evening"))

        # 21:00 — digest (kept as a separate lighter push)
        digest_t = now.replace(hour=_DIGEST_HOUR, minute=_DIGEST_MINUTE,
                               second=0, microsecond=0)
        if digest_t <= now:
            digest_t += datetime.timedelta(days=1)
        candidates.append((digest_t, "digest"))

        next_fire, next_kind = min(candidates, key=lambda x: x[0])
        wait_s = min((next_fire - now).total_seconds(), 30)

        log.info("Next scheduled: %s at %s  (%.0f s from now)",
                 next_kind, next_fire.strftime("%H:%M %Z"),
                 (next_fire - now).total_seconds())

        try:
            await asyncio.sleep(wait_s)
        except asyncio.CancelledError:
            log.info("Scheduler loop cancelled — shutting down")
            return

        await _fire_due_reminders(bot)

        now_after = datetime.datetime.now(tz=_LOCAL_TZ)
        if now_after >= next_fire:
            if next_kind == "briefing:today":
                await _send_briefing(bot, "today")
            elif next_kind == "evening":
                await _send_evening_briefing(bot)
            elif next_kind == "digest":
                await _send_digest(bot)


# ── PTB lifecycle hooks ───────────────────────────────────────────────────────

async def _post_init(application: Application) -> None:
    global _scheduler_task
    _scheduler_task = asyncio.create_task(
        _scheduler_loop(application.bot),
        name="briefing_scheduler",
    )
    log.info("Briefing scheduler started  (09:00 today / 23:00 tomorrow, %s)", _LOCAL_TZ)


async def _post_shutdown(application: Application) -> None:
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            pass
    log.info("Briefing scheduler stopped")


# ── Command handlers ──────────────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    _histories[update.effective_chat.id].clear()
    await update.message.reply_text(
        f"Personal assistant online (model: {MODEL})\n\n"
        "Just send a message — or use /help to see everything I can do.\n\n"
        "Quick examples:\n"
        "  what's on my calendar today?\n"
        "  add team meeting Thursday 2pm\n"
        "  add buy bread to my tasks\n"
        "  remind me to call mum in 2 hours\n"
        "  I had 400 cal of pasta\n\n"
        "Guided wizards: /task  /event\n"
        "Full command list: /help\n"
        "Detail on one area: /help tasks  /help reminders  /help log",
    )


# ── Help text — one dict per topic, built from real registered commands ────────
# Each value is the full HTML section for that topic.
# /help          → overview (one line per topic)
# /help <topic>  → full detail for that topic

_HELP_TOPICS: dict[str, str] = {

    "calendar": (
        "📅 <b>Calendar</b>\n\n"
        "<b>Free-text (recommended):</b>\n"
        "  <i>what's on my calendar today?</i>\n"
        "  <i>add team meeting Thursday 2pm</i>\n"
        "  <i>add standup Mon–Fri 9am, remind me 10 min before</i>\n"
        "  <i>add doctor appointment Friday 10am, make it red</i>\n"
        "  <i>delete the dentist appointment on Friday</i>\n"
        "  <i>make my 2pm meeting blue</i>\n\n"
        "<b>Guided wizard:</b>\n"
        "  /event  — step-by-step (name → time → reminder → color → confirm)\n\n"
        "<b>Colors (use word or Google name):</b>\n"
        "  red/Tomato • pink/Flamingo • orange/Tangerine • yellow/Banana\n"
        "  green/Basil • mint/Sage • teal/Peacock • blue/Blueberry\n"
        "  lavender • purple/Grape • gray/Graphite\n\n"
        "<b>Notes:</b>\n"
        "  • Deletions show a preview first — confirm or cancel.\n"
        "  • Inline buttons after listing: [🗑 Delete] per event.\n"
        "  • /cancel aborts the wizard at any step."
    ),

    "tasks": (
        "✅ <b>Tasks</b>\n\n"
        "<b>Free-text (recommended):</b>\n"
        "  <i>add buy bread to my tasks</i>\n"
        "  <i>add call dentist, due tomorrow 15:00, list Work</i>\n"
        "  <i>show my tasks</i>\n"
        "  <i>mark First lesson as done</i>\n"
        "  <i>update task Buy bread, due Friday</i>\n\n"
        "<b>Guided wizard:</b>\n"
        "  /task  — step-by-step task creation (name → due date → list)\n\n"
        "<b>Lists available:</b> your Google Task lists (fetched live from your account)\n\n"
        "<b>Notes:</b>\n"
        "  • Inline buttons after listing: [✅ Done] [🗑 Delete] per task.\n"
        "  • Due datetime creates a matching Calendar event (Tasks API is date-only).\n"
        "  • /cancel aborts the wizard at any step."
    ),

    "briefings": (
        "🌅 <b>Briefings</b>\n\n"
        "<b>Automatic schedule:</b>\n"
        "  09:00 — morning briefing\n"
        "    • Weather for your default city\n"
        "    • Today's calendar events\n"
        "    • Today's tasks (overdue flagged 🔴, due-today 🟡, upcoming 📌)\n"
        "    • Today's reminders\n"
        "    • Empty-day nudge if nothing scheduled but tasks are overdue\n\n"
        "  23:00 — evening briefing\n"
        "    • Today's accomplishments (completed tasks + events)\n"
        "    • Tomorrow's calendar events\n"
        "    • Tomorrow's tasks\n"
        "    • Tomorrow's reminders\n"
        "    • Tomorrow's weather forecast\n\n"
        "  21:00 — short digest (completed tasks + today's events)\n\n"
        "<b>Manual trigger:</b>\n"
        "  /briefing today    — morning format on demand\n"
        "  /briefing tomorrow — evening format on demand\n\n"
        "<b>Graceful failure:</b> if one section can't load, the rest still sends."
    ),

    "digest": (
        "🌙 <b>Daily digest</b>\n\n"
        "<b>Automatic:</b> sent every day at 21:00.\n\n"
        "<b>Manual trigger:</b>\n"
        "  /digest  — today's accomplishment digest right now\n\n"
        "<b>Content:</b> tasks completed today + calendar events that happened today.\n\n"
        "For a full evening briefing (accomplishments + tomorrow preview), use /briefing tomorrow."
    ),

    "memory": (
        "🧠 <b>Memory</b>\n\n"
        "<b>Commands:</b>\n"
        "  /memory view           — list all stored facts\n"
        "  /memory add &lt;text&gt;  — save a fact manually\n"
        "  /memory forget &lt;id&gt; — remove a fact by its id\n"
        "  /memory clear          — wipe all memories (asks confirmation)\n\n"
        "<b>Free-text:</b>\n"
        "  <i>remember that I prefer short replies</i>\n"
        "  <i>note that my doctor's name is Cohen</i>\n\n"
        "<b>Notes:</b>\n"
        "  • Memories are injected into every agent call.\n"
        "  • Credentials and secrets are never stored."
    ),

    "weather": (
        "🌤 <b>Weather</b>\n\n"
        "<b>Commands:</b>\n"
        "  /weather                    — current default city\n"
        "  /weather &lt;city&gt;           — one-off query for any city\n"
        "  /weather setdefault &lt;city&gt; — save a new default city\n\n"
        "<b>Examples:</b>\n"
        "  /weather Tel Aviv\n"
        "  /weather setdefault Haifa\n\n"
        "<b>Notes:</b>\n"
        "  • Powered by Open-Meteo (no API key, free).\n"
        "  • Morning briefing uses the stored default automatically."
    ),

    "search": (
        "🔍 <b>Web search</b>\n\n"
        "<b>Free-text only — no slash command needed:</b>\n"
        "  <i>what is the capital of Uzbekistan?</i>\n"
        "  <i>latest news about GPT-5</i>\n"
        "  <i>current price of Bitcoin</i>\n\n"
        "<b>Notes:</b>\n"
        "  • Powered by DuckDuckGo (free, no API key).\n"
        "  • Results are summarised by the model — not raw links."
    ),

    "gmail": (
        "📬 <b>Gmail (read-only)</b>\n\n"
        "<b>Commands:</b>\n"
        "  /inbox        — show up to 10 unread messages\n"
        "  /inbox 20     — show up to 20 unread messages\n\n"
        "<b>Free-text:</b>\n"
        "  <i>check my inbox</i>\n"
        "  <i>find emails from boss@example.com</i>\n"
        "  <i>search email subject:invoice</i>\n\n"
        "<b>Notes:</b>\n"
        "  • Read-only scope — no sending or deleting.\n"
        "  • Urgent messages are flagged automatically (keyword scan)."
    ),

    "voice": (
        "🎤 <b>Voice input</b>\n\n"
        "Send any voice message — it is transcribed locally (Whisper small model, CPU) "
        "and processed exactly like a typed message.\n\n"
        "<b>Notes:</b>\n"
        "  • The transcribed text is echoed back before the reply.\n"
        "  • First use downloads the Whisper model (~460 MB, one-time).\n"
        "  • Supports OGG/Opus (Telegram's default voice format)."
    ),

    "templates": (
        "📋 <b>Task templates</b>\n\n"
        "<b>Commands:</b>\n"
        "  /template list               — show all saved templates\n"
        "  /template add &lt;name&gt;     — start guided creation (tasks one per line, /done to save)\n"
        "  /template run &lt;name&gt;     — create all tasks from the template right now\n"
        "  /template delete &lt;name&gt;  — delete a template\n\n"
        "<b>Example:</b>\n"
        "  /template add Weekly Review\n"
        "  → <i>type tasks, one per line</i>\n"
        "  /done\n"
        "  /template run Weekly Review  → creates all tasks in Google Tasks\n\n"
        "<b>Notes:</b>\n"
        "  • /cancel aborts an in-progress template creation.\n"
        "  • Tasks are created without due dates — set them after if needed."
    ),

    "reminders": (
        "⏰ <b>Reminders</b>\n\n"
        "<b>Free-text (recommended):</b>\n"
        "  <i>remind me to take the laundry out in 30 minutes</i>\n"
        "  <i>remind me about the meeting tomorrow at 9am</i>\n\n"
        "<b>Commands:</b>\n"
        "  /reminders             — list all pending reminders\n"
        "  /reminders cancel &lt;id&gt; — cancel a pending reminder\n\n"
        "<b>Notes:</b>\n"
        "  • Reminders survive bot restarts (stored in SQLite).\n"
        "  • Checked every 30 seconds.\n"
        "  • Timezone: auto-detected from system locale."
    ),

    "log": (
        "📊 <b>Food &amp; habit log</b>\n\n"
        "<b>Quick commands:</b>\n"
        "  /log 350 pasta          — log 350 kcal of pasta\n"
        "  /log coffee             — log coffee (no calorie count)\n"
        "  /log habit gym          — log a habit\n"
        "  /log today              — show today's full log\n"
        "  /log 2026-06-07         — show log for a specific date\n\n"
        "<b>Free-text:</b>\n"
        "  <i>I had 450 calories of chicken and rice</i>\n"
        "  <i>I went for a run today</i>\n"
        "  <i>what did I eat today?</i>\n"
        "  <i>show my log for this week</i>\n\n"
        "<b>Notes:</b>\n"
        "  • Neutral log-and-recall only — no targets, advice, or commentary.\n"
        "  • Calorie count is optional."
    ),
}

# Short one-liner per topic for the overview screen
_HELP_OVERVIEW_LINES: list[tuple[str, str]] = [
    ("calendar",  "📅 Calendar   — events, reminders, delete-with-confirm"),
    ("tasks",     "✅ Tasks      — add/complete/update, list per task-list"),
    ("briefings", "🌅 Briefings  — 09:00 morning + 23:00 evening + 21:00 digest"),
    ("digest",    "🌙 Digest     — /digest on-demand, auto 21:00"),
    ("memory",    "🧠 Memory     — persistent facts the assistant remembers"),
    ("weather",   "🌤 Weather    — /weather [city], setdefault"),
    ("search",    "🔍 Search     — web search via DuckDuckGo"),
    ("gmail",     "📬 Gmail      — /inbox, search (read-only)"),
    ("voice",     "🎤 Voice      — send a voice message to talk"),
    ("templates", "📋 Templates  — reusable task bundles (/template)"),
    ("reminders", "⏰ Reminders  — 'remind me in 2h', /reminders"),
    ("log",       "📊 Log        — food & habit logging (/log)"),
]

_HELP_OVERVIEW = (
    "<b>Personal Assistant — commands &amp; features</b>\n\n"
    + "\n".join(f"  {line}" for _, line in _HELP_OVERVIEW_LINES)
    + "\n\n"
    "<b>Get details:</b> /help &lt;topic&gt;\n"
    "  e.g. /help tasks  •  /help reminders  •  /help log\n\n"
    "<b>Other:</b>\n"
    "  /start   — welcome + reset history\n"
    "  /clear   — clear conversation history\n"
    "  /cancel  — abort any active wizard\n"
    "  /done    — finish template task entry"
)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    args  = context.args or []
    topic = args[0].lower() if args else ""
    if topic and topic in _HELP_TOPICS:
        await update.message.reply_text(_HELP_TOPICS[topic], parse_mode="HTML")
    elif topic:
        valid = ", ".join(k for k, _ in _HELP_OVERVIEW_LINES)
        await update.message.reply_text(
            f"Unknown topic <b>{topic}</b>.\n\nValid topics: {valid}",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(_HELP_OVERVIEW, parse_mode="HTML")


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    _histories[update.effective_chat.id].clear()
    await update.message.reply_text("Conversation history cleared.")


async def briefing_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    args  = context.args
    which = args[0].lower() if args else ""
    if which not in ("today", "tomorrow"):
        await update.message.reply_text(
            "Usage:\n"
            "  /briefing today    — morning format (weather + today's events/tasks/reminders)\n"
            "  /briefing tomorrow — evening format (today's wins + tomorrow preview)"
        )
        return
    await update.message.reply_text("Fetching…")
    if which == "tomorrow":
        await _send_evening_briefing(context.bot, chat_id=update.effective_chat.id)
    else:
        await _send_briefing(context.bot, "today", chat_id=update.effective_chat.id)


async def digest_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text("Building digest…")
    loop = asyncio.get_running_loop()
    try:
        text = await loop.run_in_executor(_executor, build_digest)
    except Exception as exc:
        await update.message.reply_text(f"⚠️ Digest error: {exc}")
        return
    for chunk in _split_message(text):
        await update.message.reply_text(chunk)


# ── Free-text message handler ─────────────────────────────────────────────────

async def _run_agent_and_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_text: str,
) -> None:
    """Shared core: run agent for user_text and send the reply with any inline buttons."""
    chat_id = update.effective_chat.id
    history = _histories[chat_id]

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(
        _typing_loop(chat_id, context.bot, stop_typing)
    )

    try:
        loop  = asyncio.get_running_loop()
        reply = await loop.run_in_executor(
            _executor,
            lambda: run_agent(user_text, history=list(history), chat_id=chat_id),
        )
    except Exception as exc:
        log.exception("run_agent raised an unexpected exception")
        reply = f"⚠️ Error: {type(exc).__name__}: {exc}"
    finally:
        stop_typing.set()
        await typing_task

    log.info("Assistant reply: %s", reply[:80])

    history.append({"role": "user",      "content": user_text})
    history.append({"role": "assistant", "content": reply})
    if len(history) > MAX_HISTORY:
        _histories[chat_id] = history[-MAX_HISTORY:]

    # ── Attach inline buttons if the agent populated the side-channel ─────────
    btn_payload = _button_payloads.pop(chat_id, None)
    keyboard: InlineKeyboardMarkup | None = None

    if btn_payload:
        btype = btn_payload.get("type")
        bdata = btn_payload.get("data")
        if btype == "tasks" and bdata:
            keyboard = _task_list_keyboard(bdata)
        elif btype == "events" and bdata:
            keyboard = _event_list_keyboard(bdata)
        elif btype == "created_task" and bdata:
            tid = bdata.get("id")
            lid_name = bdata.get("list", "")
            try:
                lists = await asyncio.get_running_loop().run_in_executor(
                    _executor, _get_task_lists
                )
                lid = next(
                    (tl["id"] for tl in lists if tl["title"] == lid_name),
                    None,
                )
                if tid and lid:
                    keyboard = _undo_task_keyboard(tid, lid, lid_name)
            except Exception:
                pass
        elif btype == "created_event" and bdata:
            eid   = bdata.get("id")
            etitle = bdata.get("summary", "")
            if eid:
                keyboard = _undo_event_keyboard(eid, etitle)

    chunks = _split_message(reply)
    for i, chunk in enumerate(chunks):
        if i == len(chunks) - 1 and keyboard:
            await update.message.reply_text(chunk, reply_markup=keyboard)
        else:
            await update.message.reply_text(chunk)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ALLOWED_USER_ID:
        log.warning("Ignored message from user %d", update.effective_user.id)
        return
    # If /template add flow is active, collect task lines instead of calling agent
    if context.user_data.get("tmpl_state") == _TMPL_AWAIT_TASKS:
        await _template_task_collector(update, context)
        return
    user_text = update.message.text.strip()
    log.info("User (%d): %s", update.effective_user.id, user_text[:80])
    await _run_agent_and_reply(update, context, user_text)


async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    import tempfile, os, html as _html
    from whisper_stt import transcribe

    voice = update.message.voice
    await update.message.reply_text("🎤 Transcribing…")

    # Download voice file (OGG/Opus from Telegram)
    tg_file = await context.bot.get_file(voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        await tg_file.download_to_drive(tmp_path)
        text = await asyncio.to_thread(transcribe, tmp_path)
    except Exception as exc:
        await update.message.reply_text(f"⚠️ Transcription error: {exc}")
        return
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if not text:
        await update.message.reply_text("⚠️ Couldn't hear anything — try again.")
        return

    log.info("Voice transcribed (%d): %s", update.effective_user.id, text[:80])
    # Echo what was heard so the user can see what was understood
    await update.message.reply_text(f"🎤 <i>{_html.escape(text)}</i>", parse_mode="HTML")
    await _run_agent_and_reply(update, context, text)


# ══════════════════════════════════════════════════════════════════════════════
# INLINE BUTTON SYSTEM
# ══════════════════════════════════════════════════════════════════════════════
# Telegram callback_data is capped at 64 bytes. We use a short integer key
# into a local registry that stores the full payload dict.
# Registry entries are ephemeral (in-process memory), keyed by a counter.

import itertools as _itertools

_btn_counter = _itertools.count(1)
_btn_registry: dict[int, dict] = {}   # key → payload


def _reg(payload: dict) -> str:
    """Store payload in registry, return a compact callback_data key string."""
    key = next(_btn_counter)
    _btn_registry[key] = payload
    return str(key)


def _task_list_keyboard(tasks: list[dict]) -> InlineKeyboardMarkup | None:
    """
    Build one row of buttons per task: [✅ Done] [🗑 Delete].
    Tasks must have 'id' and 'list_id' fields (from list_tasks()).
    Returns None if no tasks have usable ids.
    """
    rows = []
    for t in tasks:
        tid  = t.get("id")
        lid  = t.get("list_id")
        lname = t.get("list", "Tasks")
        title = t.get("title", "?")[:30]
        if not tid or not lid:
            continue
        done_key = _reg({"action": "task_done",        "task_id": tid, "list_id": lid, "list_name": lname, "title": title})
        del_key  = _reg({"action": "task_del_preview",  "task_id": tid, "list_id": lid, "list_name": lname, "title": title})
        rows.append([
            InlineKeyboardButton(f"✅ {title}", callback_data=done_key),
            InlineKeyboardButton("🗑",           callback_data=del_key),
        ])
    return InlineKeyboardMarkup(rows) if rows else None


def _event_list_keyboard(events: list[dict]) -> InlineKeyboardMarkup | None:
    """
    Build one row of buttons per timed event: [🗑 Delete <title>].
    Events must have 'id' field (from list_calendar_events()).
    """
    rows = []
    for e in events:
        eid   = e.get("id")
        title = e.get("title", "?")[:30]
        day   = e.get("day", "")
        if not eid:
            continue
        del_key = _reg({"action": "event_del_preview", "event_id": eid, "title": title, "day": day})
        rows.append([
            InlineKeyboardButton(f"🗑 {title}", callback_data=del_key),
        ])
    return InlineKeyboardMarkup(rows) if rows else None


def _undo_task_keyboard(task_id: str, list_id: str, list_name: str) -> InlineKeyboardMarkup:
    key = _reg({"action": "undo_task", "task_id": task_id, "list_id": list_id, "list_name": list_name})
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩ Undo", callback_data=key)]])


def _undo_event_keyboard(event_id: str, title: str) -> InlineKeyboardMarkup:
    key = _reg({"action": "undo_event", "event_id": event_id, "title": title})
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩ Undo", callback_data=key)]])


async def _callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dispatch inline keyboard button presses."""
    query = update.callback_query
    if query.from_user.id != ALLOWED_USER_ID:
        await query.answer("Not authorised.")
        return

    await query.answer()   # clear the loading spinner

    key_str = query.data
    if not key_str or not key_str.isdigit():
        await query.edit_message_text("⚠️ Unknown button.")
        return

    payload = _btn_registry.pop(int(key_str), None)
    if payload is None:
        await query.edit_message_text("⚠️ This button has expired.")
        return

    action = payload.get("action", "")
    loop   = asyncio.get_running_loop()

    # ── Task: mark done ───────────────────────────────────────────────────────
    if action == "task_done":
        result = await loop.run_in_executor(
            _executor,
            lambda: _complete_task_by_id(
                payload["task_id"], payload["list_id"], payload["list_name"]
            ),
        )
        if "error" in result:
            await query.edit_message_text(f"⚠️ {result['error']}")
        else:
            await query.edit_message_text(
                f"✅ Done: <b>{payload['title']}</b>  [{payload['list_name']}]",
                parse_mode="HTML",
            )

    # ── Task: delete preview ──────────────────────────────────────────────────
    elif action == "task_del_preview":
        confirm_key = _reg({
            "action": "task_del_confirm",
            "task_id": payload["task_id"], "list_id": payload["list_id"],
            "list_name": payload["list_name"], "title": payload["title"],
        })
        cancel_key = _reg({"action": "cancel"})
        await query.edit_message_text(
            f"⚠️ Delete task: <b>{payload['title']}</b>  [{payload['list_name']}]?\n\n"
            f"This cannot be undone.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Yes, delete", callback_data=confirm_key),
                InlineKeyboardButton("Cancel",       callback_data=cancel_key),
            ]]),
        )

    # ── Task: delete confirmed ────────────────────────────────────────────────
    elif action == "task_del_confirm":
        result = await loop.run_in_executor(
            _executor,
            lambda: _delete_task_by_id(
                payload["task_id"], payload["list_id"], payload["list_name"]
            ),
        )
        if "error" in result:
            await query.edit_message_text(f"⚠️ {result['error']}")
        else:
            await query.edit_message_text(
                f"🗑 Deleted: <b>{payload['title']}</b>  [{payload['list_name']}]",
                parse_mode="HTML",
            )

    # ── Event: delete preview ─────────────────────────────────────────────────
    elif action == "event_del_preview":
        confirm_key = _reg({
            "action": "event_del_confirm",
            "event_id": payload["event_id"], "title": payload["title"],
        })
        cancel_key = _reg({"action": "cancel"})
        await query.edit_message_text(
            f"⚠️ Delete event: <b>{payload['title']}</b>  ({payload.get('day','')})?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Yes, delete", callback_data=confirm_key),
                InlineKeyboardButton("Cancel",       callback_data=cancel_key),
            ]]),
        )

    # ── Event: delete confirmed ───────────────────────────────────────────────
    elif action == "event_del_confirm":
        event_id = payload["event_id"]
        title    = payload["title"]
        try:
            cal_svc, _ = await loop.run_in_executor(_executor, _get_google_services)
            await loop.run_in_executor(
                _executor,
                lambda: cal_svc.events().delete(calendarId="primary", eventId=event_id).execute(),
            )
            result_msg = f"🗑 Deleted event: <b>{title}</b>"
        except Exception as exc:
            result_msg = f"⚠️ Delete failed: {exc}"
        await query.edit_message_text(result_msg, parse_mode="HTML")

    # ── Undo: task creation ───────────────────────────────────────────────────
    elif action == "undo_task":
        result = await loop.run_in_executor(
            _executor,
            lambda: _delete_task_by_id(
                payload["task_id"], payload["list_id"], payload["list_name"]
            ),
        )
        if "error" in result:
            await query.edit_message_text(f"⚠️ Undo failed: {result['error']}")
        else:
            await query.edit_message_text("↩ Task creation undone.")

    # ── Undo: event creation ──────────────────────────────────────────────────
    elif action == "undo_event":
        event_id = payload["event_id"]
        title    = payload["title"]
        try:
            cal_svc, _ = await loop.run_in_executor(_executor, _get_google_services)
            await loop.run_in_executor(
                _executor,
                lambda: cal_svc.events().delete(calendarId="primary", eventId=event_id).execute(),
            )
            await query.edit_message_text(f"↩ Event «{title}» creation undone.")
        except Exception as exc:
            await query.edit_message_text(f"⚠️ Undo failed: {exc}")

    # ── Cancel ────────────────────────────────────────────────────────────────
    elif action == "cancel":
        await query.edit_message_text("Cancelled.")

    else:
        await query.edit_message_text("⚠️ Unknown action.")


# ── /inbox command handler ───────────────────────────────────────────────────

def _format_inbox(messages: list[dict]) -> str:
    """Deterministic formatter — never calls LLM, all content from API."""
    from html import escape as _e
    if not messages:
        return "📭 No unread messages in your inbox."

    urgent   = [m for m in messages if m.get("is_urgent")]
    normal   = [m for m in messages if not m.get("is_urgent")]
    lines: list[str] = [f"📬 <b>Inbox — {len(messages)} unread</b>"]

    if urgent:
        lines.append("")
        lines.append("🔴 <b>Flagged as urgent:</b>")
        for m in urgent:
            if "error" in m:
                lines.append(f"   ⚠️ {_e(m['error'])}")
                continue
            sender  = _e(m.get("sender", "?"))
            subject = _e(m.get("subject", "(no subject)"))
            date    = _e(m.get("date", ""))
            snippet = _e(m.get("snippet", "")[:120])
            lines.append(f"   • <b>{subject}</b>")
            lines.append(f"     From: {sender}  [{date}]")
            if snippet:
                lines.append(f"     {snippet}")

    if normal:
        lines.append("")
        lines.append("📩 <b>Other unread:</b>")
        for m in normal:
            if "error" in m:
                lines.append(f"   ⚠️ {_e(m['error'])}")
                continue
            sender  = _e(m.get("sender", "?"))
            subject = _e(m.get("subject", "(no subject)"))
            date    = _e(m.get("date", ""))
            snippet = _e(m.get("snippet", "")[:100])
            lines.append(f"   • <b>{subject}</b>  [{date}]")
            lines.append(f"     {sender}")
            if snippet:
                lines.append(f"     {snippet}")

    return "\n".join(lines)


async def inbox_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    args = context.args or []
    try:
        max_msg = int(args[0]) if args and args[0].isdigit() else 10
    except ValueError:
        max_msg = 10

    await update.message.reply_text("Fetching inbox…")
    try:
        from gmail_tools import summarize_unread
        messages = await asyncio.to_thread(summarize_unread, max_msg)
    except Exception as exc:
        await update.message.reply_text(f"⚠️ Gmail error: {exc}")
        return

    text = _format_inbox(messages)
    for chunk in _split_message(text):
        await update.message.reply_text(chunk, parse_mode="HTML")


# ── /template command handler ─────────────────────────────────────────────────
#
# Usage:
#   /template list
#   /template add <name>        → guided: bot asks for tasks
#   /template run <name>
#   /template delete <name>
#
# "run" creates all tasks in the template via the Tasks API (no due date).
# Conversation states for the "add" flow:

_TMPL_AWAIT_TASKS = "tmpl_await_tasks"   # waiting for user to type the task list


async def template_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    from html import escape as _e
    import templates as _tpl
    from tools import create_task as _create_task

    args = context.args or []
    sub  = args[0].lower() if args else ""

    # ── /template list ────────────────────────────────────────────────────────
    if sub == "list" or not sub:
        items = _tpl.list_templates()
        if not items:
            await update.message.reply_text(
                "No templates saved yet.\n"
                "Create one with: /template add <name>"
            )
            return
        lines = ["📋 <b>Saved templates:</b>"]
        for it in items:
            lines.append(
                f"  • <b>{_e(it['name'])}</b>  "
                f"({it['task_count']} task{'s' if it['task_count'] != 1 else ''})"
            )
        lines.append("")
        lines.append("Run with: /template run &lt;name&gt;")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    # ── /template add <name> ──────────────────────────────────────────────────
    if sub == "add":
        name = " ".join(args[1:]).strip()
        if not name:
            await update.message.reply_text(
                "Usage: /template add <name>\nExample: /template add Morning Routine"
            )
            return
        context.user_data["tmpl_name"] = name
        await update.message.reply_text(
            f"Creating template <b>{_e(name)}</b>.\n\n"
            "Send the task titles, one per line:\n"
            "<i>Example:\nBuy groceries\nCall dentist\nReview weekly goals</i>\n\n"
            "When done, send /done — or /cancel to abort.",
            parse_mode="HTML",
        )
        context.user_data["tmpl_state"] = _TMPL_AWAIT_TASKS
        return

    # ── /template run <name> ──────────────────────────────────────────────────
    if sub == "run":
        name = " ".join(args[1:]).strip()
        if not name:
            await update.message.reply_text("Usage: /template run <name>")
            return
        tasks = _tpl.get_template(name)
        if not tasks:
            await update.message.reply_text(f"No template named '{_e(name)}' found.")
            return
        await update.message.reply_text(f"Creating {len(tasks)} task(s) from <b>{_e(name)}</b>…", parse_mode="HTML")
        created, failed = [], []
        for title in tasks:
            try:
                result = await asyncio.to_thread(_create_task, title)
                if result.get("status") == "created":
                    created.append(title)
                else:
                    failed.append(title)
            except Exception as exc:
                failed.append(f"{title} ({exc})")
        lines = [f"✅ Created {len(created)} task(s) from <b>{_e(name)}</b>:"]
        for t in created:
            lines.append(f"  • {_e(t)}")
        if failed:
            lines.append("")
            lines.append("⚠️ Failed:")
            for t in failed:
                lines.append(f"  • {_e(t)}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    # ── /template delete <name> ───────────────────────────────────────────────
    if sub == "delete":
        name = " ".join(args[1:]).strip()
        if not name:
            await update.message.reply_text("Usage: /template delete <name>")
            return
        deleted = _tpl.delete_template(name)
        if deleted:
            await update.message.reply_text(f"🗑 Template <b>{_e(name)}</b> deleted.", parse_mode="HTML")
        else:
            await update.message.reply_text(f"No template named '{_e(name)}' found.")
        return

    await update.message.reply_text(
        "Usage:\n"
        "  /template list\n"
        "  /template add <name>\n"
        "  /template run <name>\n"
        "  /template delete <name>"
    )


async def template_done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Finishes the /template add flow when user sends /done."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    if context.user_data.get("tmpl_state") != _TMPL_AWAIT_TASKS:
        await update.message.reply_text("No template in progress. Use /template add <name> to start.")
        return

    from html import escape as _e
    import templates as _tpl

    name  = context.user_data.pop("tmpl_name", "")
    tasks_raw = context.user_data.pop("tmpl_tasks", [])
    context.user_data.pop("tmpl_state", None)

    if not tasks_raw:
        await update.message.reply_text("No tasks entered — template not saved. Use /template add <name> to try again.")
        return

    try:
        _tpl.save_template(name, tasks_raw)
    except ValueError as exc:
        await update.message.reply_text(f"⚠️ {exc}")
        return

    from html import escape as _e
    lines = [f"✅ Template <b>{_e(name)}</b> saved with {len(tasks_raw)} task(s):"]
    for t in tasks_raw:
        lines.append(f"  • {_e(t)}")
    lines.append("")
    lines.append(f"Run it with: /template run {_e(name)}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def _template_task_collector(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Collects task lines while /template add flow is active."""
    if context.user_data.get("tmpl_state") != _TMPL_AWAIT_TASKS:
        return False   # signal: not handled
    text = update.message.text.strip()
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    existing = context.user_data.setdefault("tmpl_tasks", [])
    existing.extend(lines)
    count = len(existing)
    await update.message.reply_text(
        f"Added {len(lines)} task(s) — {count} total so far.\n"
        "Send more tasks, or /done to save."
    )


# ── /reminders command handler ────────────────────────────────────────────────
#
# Usage:
#   /reminders          — list pending reminders
#   /reminders cancel <id>  — delete a pending reminder

async def reminders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    from html import escape as _e
    import reminders as _rem

    chat_id = update.effective_chat.id
    args    = context.args or []

    if args and args[0].lower() == "cancel":
        if len(args) < 2 or not args[1].isdigit():
            await update.message.reply_text("Usage: /reminders cancel <id>")
            return
        rid = int(args[1])
        if _rem.delete(rid, chat_id):
            await update.message.reply_text(f"🗑 Reminder #{rid} cancelled.")
        else:
            await update.message.reply_text(f"No pending reminder #{rid} found.")
        return

    pending = _rem.get_pending_for_chat(chat_id)
    if not pending:
        await update.message.reply_text("No pending reminders.\nAsk me to remind you about something!")
        return

    lines = [f"⏰ <b>{len(pending)} pending reminder(s):</b>"]
    for r in pending:
        # Show fire_at in local time
        try:
            dt = datetime.datetime.fromisoformat(r["fire_at"]).replace(
                tzinfo=datetime.timezone.utc
            ).astimezone(_LOCAL_TZ)
            time_str = dt.strftime("%a %d %b, %H:%M")
        except Exception:
            time_str = r["fire_at"]
        lines.append(f"  <b>#{r['id']}</b>  {time_str}")
        lines.append(f"     {_e(r['message'])}")
    lines.append("")
    lines.append("Cancel one with: /reminders cancel &lt;id&gt;")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ── /log command handler ──────────────────────────────────────────────────────
#
# Quick log without typing full sentences.
# Usage:
#   /log <N> <item>           → log N calories of item    e.g. /log 350 pasta
#   /log <item>               → log item without calories  e.g. /log coffee
#   /log habit <description>  → log a habit               e.g. /log habit gym
#   /log today                → show today's log
#   /log <YYYY-MM-DD>         → show log for that date

async def log_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    from html import escape as _e
    import log_store as _ls
    import datetime as _dt

    args = context.args or []
    today = _dt.date.today().isoformat()

    if not args or args[0].lower() == "today":
        data = _ls.get_log(today)
        await update.message.reply_text(_format_log(data), parse_mode="HTML")
        return

    # /log <YYYY-MM-DD>
    if len(args) == 1 and args[0].count("-") == 2:
        data = _ls.get_log(args[0])
        await update.message.reply_text(_format_log(data), parse_mode="HTML")
        return

    # /log habit <description>
    if args[0].lower() == "habit":
        habit = " ".join(args[1:]).strip()
        if not habit:
            await update.message.reply_text("Usage: /log habit <description>")
            return
        _ls.log_habit(today, habit)
        await update.message.reply_text(f"✅ Logged habit: {_e(habit)}")
        return

    # /log <N> <item>  or  /log <item>
    if args[0].isdigit():
        cal   = int(args[0])
        item  = " ".join(args[1:]).strip() or "unspecified"
        _ls.log_calories(today, item, cal)
        await update.message.reply_text(f"✅ Logged: {_e(item)} — {cal} kcal")
    else:
        item = " ".join(args).strip()
        _ls.log_calories(today, item, None)
        await update.message.reply_text(f"✅ Logged: {_e(item)}")


def _format_log(data: dict) -> str:
    from html import escape as _e
    date_str = data.get("date") or f"{data.get('start')} – {data.get('end')}"
    lines = [f"📋 <b>Log — {_e(date_str)}</b>"]

    cal_entries = data.get("calories", [])
    if cal_entries:
        lines.append("")
        lines.append("🍽 <b>Food / drink:</b>")
        for e in cal_entries:
            cal_part = f"  ({e['calories']} kcal)" if e.get("calories") else ""
            lines.append(f"  • {_e(e['item'])}{cal_part}")
        total = data.get("total_cal")
        if total:
            lines.append(f"  <i>Total logged: {total} kcal</i>")
    else:
        lines.append("\n🍽 No food entries.")

    hab_entries = data.get("habits", [])
    if hab_entries:
        lines.append("")
        lines.append("✅ <b>Habits:</b>")
        for e in hab_entries:
            lines.append(f"  • {_e(e['habit'])}")
    else:
        lines.append("\n✅ No habit entries.")

    return "\n".join(lines)


# ── /weather command handler ──────────────────────────────────────────────────
#
# Usage:
#   /weather                    → weather for stored default city
#   /weather <city>             → weather for that city (not saved as default)
#   /weather setdefault <city>  → save <city> as the new default

async def weather_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    args = context.args or []

    # ── /weather setdefault <city> ────────────────────────────────────────────
    if args and args[0].lower() == "setdefault":
        new_city = " ".join(args[1:]).strip()
        if not new_city:
            await update.message.reply_text(
                "Usage: /weather setdefault &lt;city name&gt;\n"
                f"Current default: <b>{_weather.get_city()}</b>",
                parse_mode="HTML",
            )
            return
        await update.message.reply_text("Checking city…")
        loop = asyncio.get_running_loop()
        msg = await loop.run_in_executor(_executor, lambda: _weather.set_city(new_city))
        await update.message.reply_text(msg)
        return

    # ── /weather [city] ───────────────────────────────────────────────────────
    city_override = " ".join(args).strip() if args else None
    label = city_override or _weather.get_city()
    await update.message.reply_text(f"Fetching weather for {label}…")
    loop = asyncio.get_running_loop()
    block = await loop.run_in_executor(
        _executor,
        lambda: _weather.get_weather(include_tomorrow=True, city=city_override),
    )
    await update.message.reply_text(block)


# ── /memory command handler ───────────────────────────────────────────────────

async def memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    chat_id = update.effective_chat.id
    args    = context.args or []
    sub     = args[0].lower() if args else "view"

    if sub == "view":
        mems = _memory.get_all()
        if not mems:
            await update.message.reply_text("No memories stored.")
            return
        lines = ["<b>Stored memories:</b>\n"]
        for m in mems:
            lines.append(
                f"  <b>[{m['id']}]</b> <i>{m['category']}</i>: {m['fact']}"
                f"  <code>({m['created']})</code>"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    elif sub == "add":
        fact = " ".join(args[1:]).strip()
        if not fact:
            await update.message.reply_text("Usage: /memory add <fact text>")
            return
        result = _memory.add(fact)
        if "error" in result:
            await update.message.reply_text(f"⚠️ {result['error']}")
        else:
            await update.message.reply_text(
                f"✅ Memory saved  (id={result['id']})\n"
                f"  Fact:     {result['fact']}\n"
                f"  Category: {result['category']}\n"
                f"  Date:     {result['created']}"
            )

    elif sub == "forget":
        if not args[1:] or not args[1].isdigit():
            await update.message.reply_text("Usage: /memory forget <id>")
            return
        result = _memory.forget(int(args[1]))
        if "error" in result:
            await update.message.reply_text(f"⚠️ {result['error']}")
        else:
            d = result["deleted"]
            await update.message.reply_text(
                f"🗑 Forgotten: [{d['id']}] <i>{d['category']}</i>: {d['fact']}",
                parse_mode="HTML",
            )

    elif sub == "clear":
        count = len(_memory.get_all())
        if count == 0:
            await update.message.reply_text("No memories to clear.")
            return
        _pending_memory_clear.add(chat_id)
        await update.message.reply_text(
            f"⚠️ About to delete all <b>{count}</b> memories.\n\n"
            f"Send /memory confirm to proceed, or anything else to cancel.",
            parse_mode="HTML",
        )

    elif sub == "confirm":
        if chat_id not in _pending_memory_clear:
            await update.message.reply_text("Nothing to confirm.")
            return
        _pending_memory_clear.discard(chat_id)
        count = _memory.clear_all()
        await update.message.reply_text(f"🗑 All {count} memories cleared.")

    else:
        await update.message.reply_text(
            "<b>Memory commands:</b>\n\n"
            "  /memory view           — list all memories\n"
            "  /memory add &lt;text&gt;  — add a fact\n"
            "  /memory forget &lt;id&gt; — remove one by id\n"
            "  /memory clear          — wipe all (asks confirmation)",
            parse_mode="HTML",
        )

    # Cancel a pending clear if any non-confirm message arrived for this chat
    if sub not in ("clear", "confirm") and chat_id in _pending_memory_clear:
        _pending_memory_clear.discard(chat_id)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    # Guided flows — MUST be added before the generic MessageHandler so they
    # capture messages when a conversation is active.
    app.add_handler(task_conv)
    app.add_handler(event_conv)

    # Regular commands
    app.add_handler(CommandHandler("start",    start_command))
    app.add_handler(CommandHandler("help",     help_command))
    app.add_handler(CommandHandler("clear",    clear_command))
    app.add_handler(CommandHandler("briefing", briefing_command))
    app.add_handler(CommandHandler("digest",   digest_command))
    app.add_handler(CommandHandler("memory",   memory_command))
    app.add_handler(CommandHandler("weather",  weather_command))
    app.add_handler(CommandHandler("inbox",    inbox_command))
    app.add_handler(CommandHandler("template",  template_command))
    app.add_handler(CommandHandler("done",      template_done_command))
    app.add_handler(CommandHandler("reminders", reminders_command))
    app.add_handler(CommandHandler("log",       log_command))
    # /cancel when no flow is active
    app.add_handler(CommandHandler("cancel",   _cancel_standalone))

    # Inline button callbacks
    app.add_handler(CallbackQueryHandler(_callback_handler))

    # Free-text fallback — runs only when no guided flow is active
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, voice_handler))

    log.info("formatter=ON  rail=ON  model=%s  allowed_user=%d  flows=task,event",
             MODEL, ALLOWED_USER_ID)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
