"""
agent.py — Stage 3: LLM tool-calling loop.

Connects to Ollama's OpenAI-compatible API, defines the 5 Google tools,
and runs a loop:
  user message → model → tool calls → tool results → model → final answer

Change MODEL to switch between qwen2.5:7b and llama3.1:8b for A/B testing.
Only one model is loaded at a time.
"""

import json
import re
from datetime import date

from openai import OpenAI
import memory as _memory
import weather as _weather
from search import web_search as _web_search
from gmail_tools import summarize_unread as _summarize_unread, search_email as _search_email
import reminders as _reminders
import log_store as _log_store

# ── Config — change this one line to switch models ────────────────────────────
MODEL = "qwen2.5:7b"

# ── Ollama client (OpenAI-compatible endpoint) ─────────────────────────────────
client = OpenAI(
    base_url="http://localhost:11434/v1",
    api_key="ollama",
)

# ── Import the tool functions from Stage 2 ────────────────────────────────────
from tools import (
    list_calendar_events,
    create_calendar_event,
    set_event_reminder,
    update_event_color,
    delete_calendar_event,
    list_tasks,
    create_task,
    update_task,
    complete_task,
    color_options_text as _color_options_text,
)

def _save_memory_tool(fact: str, category: str = "general") -> dict:
    return _memory.add(fact, category)

def _log_calories_tool(item: str, calories: int | None = None, date: str | None = None) -> dict:
    return _log_store.log_calories(date or str(date.today()), item, calories)

def _log_habit_tool(habit: str, date: str | None = None) -> dict:
    return _log_store.log_habit(date or str(date.today()), habit)

def _get_log_tool(date: str | None = None, range: str | None = None) -> dict:
    if range:
        parts = range.split("/", 1)
        if len(parts) == 2:
            return _log_store.get_log_range(parts[0], parts[1])
    return _log_store.get_log(date or str(date.today()))

def _ask_clarification_tool(question: str) -> dict:
    return {"clarification_needed": True, "question": question}

# set_reminder is chat_id-aware — the real function is built inside run_agent
def _set_reminder_stub(message: str, fire_at: str) -> dict:
    return {"error": "set_reminder called outside agent context"}

TOOL_FUNCTIONS = {
    "list_calendar_events":  list_calendar_events,
    "create_calendar_event": create_calendar_event,
    "set_event_reminder":    set_event_reminder,
    "update_event_color":    update_event_color,
    "delete_calendar_event": delete_calendar_event,
    "list_tasks":            list_tasks,
    "create_task":           create_task,
    "update_task":           update_task,
    "complete_task":         complete_task,
    "save_memory":           _save_memory_tool,
    "ask_clarification":     _ask_clarification_tool,
    "web_search":            _web_search,
    "summarize_unread":      _summarize_unread,
    "search_email":          _search_email,
    "set_reminder":          _set_reminder_stub,
    "log_calories":          _log_calories_tool,
    "log_habit":             _log_habit_tool,
    "get_log":               _get_log_tool,
}

# ── Tool schemas ──────────────────────────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_calendar_events",
            "description": (
                "List calendar events for a date, keyword, or explicit date range. "
                "For week ranges use keywords ('this week', 'next week'). "
                "For explicit ranges (e.g. June 16–26) pass start as date_str and end as end_date."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date_str": {
                        "type": "string",
                        "description": (
                            "A keyword or ISO date 'YYYY-MM-DD'. "
                            "Keywords (English): 'today', 'tomorrow', 'this week', 'next week', 'week after next'. "
                            "Keywords (Russian): 'сегодня', 'завтра', 'эта неделя', 'следующая неделя', "
                            "'через неделю', 'неделя после следующей'. "
                            "DATE FORMAT IS DAY-FIRST (European): 16.6 means June 16, NOT June 4. "
                            "Convert DD.MM to ISO before passing: 16.6 → 2026-06-16, 26.6 → 2026-06-26. "
                            "For a range, put the start date here."
                        ),
                    },
                    "end_date": {
                        "type": "string",
                        "description": (
                            "Range end as ISO date 'YYYY-MM-DD'. "
                            "Required when the user specifies an explicit end date. "
                            "Convert DD.MM to ISO: 26.6 → 2026-06-26."
                        ),
                    },
                },
                "required": ["date_str"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_calendar_event",
            "description": (
                "Create a new event on the primary Google Calendar. "
                "Use reminder_minutes to set a notification before the event. "
                "Use color to set the event color (e.g. 'red', 'blue', 'Tomato')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Event title."},
                    "start": {"type": "string", "description": "Start datetime ISO 8601, e.g. '2026-06-08T14:00:00'."},
                    "end":   {"type": "string", "description": "End datetime ISO 8601, e.g. '2026-06-08T15:00:00'."},
                    "reminder_minutes": {
                        "type": "integer",
                        "description": (
                            "Minutes before the event for a popup. "
                            "Examples: 10=10 min before, 30=30 min before, 60=1 hour, 1440=1 day. "
                            "Omit to use calendar default."
                        ),
                    },
                    "color": {
                        "type": "string",
                        "description": (
                            "Optional event color. Accepts natural words like 'red', 'blue', 'green', "
                            "'yellow', 'orange', 'purple', 'pink', 'teal', 'gray', or Google names like "
                            "'Tomato', 'Blueberry', 'Basil'. If unrecognised, the tool returns an error "
                            "with the available options."
                        ),
                    },
                },
                "required": ["title", "start", "end"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_event_reminder",
            "description": (
                "Set or change the reminder/notification on an EXISTING calendar event. "
                "Use this when the user says 'remind me X before [event]' about an existing event."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Full or partial title of the event to update.",
                    },
                    "date_str": {
                        "type": "string",
                        "description": "Date or range start to find the event (ISO 'YYYY-MM-DD' or keyword).",
                    },
                    "reminder_minutes": {
                        "type": "integer",
                        "description": (
                            "Minutes before the event for the popup notification. "
                            "0=at event time, 30=30 min before, 1440=1 day before."
                        ),
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Optional range end 'YYYY-MM-DD' if the event could be on multiple days.",
                    },
                },
                "required": ["title", "date_str", "reminder_minutes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_event_color",
            "description": (
                "Change the color of an EXISTING calendar event. "
                "Use this when the user says 'make my meeting red' or 'change the color of X to blue'. "
                "Accepts natural color words ('red', 'blue', 'green', etc.) or Google names ('Tomato', 'Blueberry')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title":    {"type": "string", "description": "Full or partial event title (case-insensitive)."},
                    "date_str": {"type": "string", "description": "Date or range start to find the event ('today', 'tomorrow', 'YYYY-MM-DD')."},
                    "color":    {"type": "string", "description": "Color word or Google name, e.g. 'red', 'blue', 'Tomato', 'Blueberry'."},
                    "end_date": {"type": "string", "description": "Optional range end 'YYYY-MM-DD'."},
                },
                "required": ["title", "date_str", "color"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_calendar_event",
            "description": (
                "Delete calendar events matching a title within a date/range. "
                "TWO-STEP SAFETY: First call WITHOUT confirm (or confirm=false) to get a preview "
                "of matching events — this deletes nothing. Show the preview to the user and ask "
                "them to confirm. Only after the user explicitly says yes, call again with confirm=true."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title":    {"type": "string", "description": "Full or partial event title to match."},
                    "date_str": {"type": "string", "description": "Date or range start (keyword or ISO 'YYYY-MM-DD'; DD.MM is day-first)."},
                    "end_date": {"type": "string", "description": "Optional range end as ISO 'YYYY-MM-DD'."},
                    "confirm":  {"type": "boolean", "description": "Leave false/omitted to preview. Set true ONLY after the user confirms."},
                },
                "required": ["title", "date_str"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tasks",
            "description": (
                "List open (incomplete) tasks from Google Tasks. "
                "Without list_name, returns tasks from ALL lists. "
                "Available lists: 'Work', 'Uni', 'Gym', 'Driving', or omit for the primary list."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "list_name": {
                        "type": "string",
                        "description": (
                            "Optional. One of: 'Work', 'Uni', 'Gym', 'Driving'. "
                            "Omit to get tasks from all lists (including the primary list)."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": (
                "Create a new task in Google Tasks. "
                "For a task with ONLY a due date, use due_date. "
                "For a task with a specific time (e.g. 'tomorrow at 15:00'), use due_datetime — "
                "the task due is set to that day and a Calendar event is also created at that time "
                "because the Tasks API cannot store a time (verified). "
                "IMPORTANT: list_name must be exactly one of: 'Work', 'Uni', 'Gym', 'Driving'. "
                "Omit list_name (or pass nothing) to use the primary list. "
                "NEVER invent a list name — if the user names an unknown list, return an error."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Task title."},
                    "due_date": {
                        "type": "string",
                        "description": (
                            "Date-only due date as 'YYYY-MM-DD'. "
                            "Use when the user specifies a date but no time. "
                            "Do NOT use together with due_datetime."
                        ),
                    },
                    "due_datetime": {
                        "type": "string",
                        "description": (
                            "Datetime as 'YYYY-MM-DDTHH:MM:SS' or 'YYYY-MM-DDTHH:MM'. "
                            "Use when the user specifies both a date AND a time. "
                            "Creates task with due=that date + a Calendar event at that time. "
                            "Do NOT use together with due_date."
                        ),
                    },
                    "list_name": {
                        "type": "string",
                        "description": (
                            "Task list: 'Work', 'Uni', 'Gym', or 'Driving'. "
                            "Omit to use the primary list. "
                            "Do NOT invent list names."
                        ),
                    },
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_task",
            "description": (
                "Update an EXISTING task — change its due date, time, or title. "
                "Use this when the user says 'set it to tomorrow 9am', 'reschedule task X', "
                "'change the due date of Y', etc. "
                "Same time limitation as create_task: due_datetime creates a Calendar event."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_title": {
                        "type": "string",
                        "description": "Full or partial title of the task to update (case-insensitive).",
                    },
                    "list_name": {
                        "type": "string",
                        "description": "Optional. List to search: 'Work', 'Uni', 'Gym', 'Driving'. Omit to search all.",
                    },
                    "due_date": {
                        "type": "string",
                        "description": "New date-only due date 'YYYY-MM-DD'. Do NOT use with due_datetime.",
                    },
                    "due_datetime": {
                        "type": "string",
                        "description": (
                            "New due datetime 'YYYY-MM-DDTHH:MM'. "
                            "Sets task due to that date + creates a Calendar event at that time. "
                            "Do NOT use with due_date."
                        ),
                    },
                    "new_title": {
                        "type": "string",
                        "description": "Rename the task to this new title.",
                    },
                },
                "required": ["task_title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_task",
            "description": "Mark a task as completed, identified by its title or partial title.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_title": {
                        "type": "string",
                        "description": "Full or partial title of the task to mark as done.",
                    },
                    "list_name": {
                        "type": "string",
                        "description": "Optional list to search within. Omit to search all lists.",
                    },
                },
                "required": ["task_title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web for current or factual information not available from "
                "the user's calendar or tasks. Call this for general questions, news, "
                "definitions, prices, how-to questions, or anything requiring external knowledge. "
                "NEVER fabricate search results — always call this tool and cite sources. "
                "Summarize from the returned snippets and include source URLs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query. Be specific and concise.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Number of results to return (1-5). Default 4.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_clarification",
            "description": (
                "Ask the user a clarifying question when their request is genuinely ambiguous "
                "and cannot be resolved from context. "
                "PRIMARY USE: when the user asks to add/create something with a specific time "
                "but has NOT said whether they want a calendar event or a task — ask which. "
                "Do NOT call this if the intent is clear (see routing rules in system prompt). "
                "Do NOT call this for anything other than routing ambiguity."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": (
                            "The question to show the user. Keep it short and binary when possible. "
                            "Example: 'Add \"Call John tomorrow at 3pm\" as a calendar event or a task?'"
                        ),
                    },
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": (
                "Persist a fact or preference so it is remembered across sessions. "
                "Call this ONLY when the user explicitly says 'remember', 'note that', "
                "'always do X', 'my preference is', or similar. "
                "Never save secrets, passwords, or tokens. "
                "Never call this unless the user clearly wants something remembered."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "fact": {
                        "type": "string",
                        "description": (
                            "The fact or preference to store, stated as a concise one-line sentence. "
                            "Example: 'default new tasks to the Work list'."
                        ),
                    },
                    "category": {
                        "type": "string",
                        "description": (
                            "Short tag: 'preference', 'instruction', 'context', 'user', or 'general'. "
                            "Default: 'general'."
                        ),
                    },
                },
                "required": ["fact"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "summarize_unread",
            "description": (
                "Fetch the user's unread Gmail inbox messages and return real metadata "
                "(sender, subject, date, snippet, urgency flag). "
                "Call this when the user asks about their email, inbox, or unread messages. "
                "NEVER fabricate email content — every field comes from the live API."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "max_messages": {
                        "type": "integer",
                        "description": "Maximum number of unread messages to return (default 10, max 25).",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_email",
            "description": (
                "Search Gmail using a query string (same syntax as the Gmail search box). "
                "Examples: 'from:boss@example.com', 'subject:invoice', 'is:unread after:2026/06/01'. "
                "Call this when the user asks to find specific emails. "
                "NEVER fabricate results."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Gmail search query, e.g. 'from:someone subject:meeting is:unread'.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max messages to return (default 10, max 25).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_reminder",
            "description": (
                "Schedule a reminder message to be sent to the user at a specific future time. "
                "Use this when the user asks to be reminded about something later. "
                "Convert relative times ('in 2 hours', 'tomorrow at 9am') to absolute ISO 8601 "
                "datetimes based on today's date and the user's local timezone."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "The reminder text to send, e.g. 'Take the laundry out of the machine'.",
                    },
                    "fire_at": {
                        "type": "string",
                        "description": (
                            "ISO 8601 datetime when to fire the reminder, e.g. '2026-06-09T09:00:00'. "
                            "Use the user's local time. "
                            "Must be in the future."
                        ),
                    },
                },
                "required": ["message", "fire_at"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_calories",
            "description": (
                "Log a food or drink entry for today (or a specified date). "
                "Call this when the user mentions eating, drinking, or tracking food. "
                "NEVER add diet advice, compute totals vs goals, or comment on the amount."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item":     {"type": "string",  "description": "Food or drink description, e.g. 'pasta', 'coffee with milk'."},
                    "calories": {"type": "integer", "description": "Calorie count (optional — omit if unknown)."},
                    "date":     {"type": "string",  "description": "Date YYYY-MM-DD. Omit to use today."},
                },
                "required": ["item"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_habit",
            "description": (
                "Log a habit or activity for today (or a specified date). "
                "Call this when the user mentions completing a recurring activity like gym, running, "
                "drinking water, reading, etc. NEVER evaluate or comment on the habit."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "habit": {"type": "string", "description": "Habit or activity description, e.g. 'gym', 'ran 5km', 'drank 2L water'."},
                    "date":  {"type": "string", "description": "Date YYYY-MM-DD. Omit to use today."},
                },
                "required": ["habit"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_log",
            "description": (
                "Retrieve calorie and habit log entries. Call this when the user asks "
                "what they ate, logged, or tracked. Return the raw data — no targets, "
                "no advice, no evaluation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date":  {"type": "string", "description": "Single date YYYY-MM-DD. Omit to use today."},
                    "range": {"type": "string", "description": "Date range as 'YYYY-MM-DD/YYYY-MM-DD' for multi-day queries (e.g. this week)."},
                },
                "required": [],
            },
        },
    },
]


# ── Startup banner ─────────────────────────────────────────────────────────────
# Printed once at import time so the process log shows what code is loaded.
print(f"[agent] formatter=ON  rail=ON  model={MODEL}", flush=True)


# ── Pending-delete registry ────────────────────────────────────────────────────
# Tracks which chat has been shown a delete preview and is awaiting confirmation.
# keyed by chat_id (int).  Cleared once the confirmed delete executes.
_pending_deletes: dict[int, dict] = {}

# ── Button-payload side-channel ───────────────────────────────────────────────
# run_agent populates this after a short-circuit so telegram_bot.py can attach
# inline buttons without the agent having to know about Telegram internals.
# Structure: { chat_id: {"type": "tasks"|"events"|"created_task"|"created_event",
#                        "data": <list[dict] | dict>} }
_button_payloads: dict[int, dict] = {}


# ── Calendar result formatter ─────────────────────────────────────────────────

_RU_DAYS = {
    "Monday": "Понедельник", "Tuesday": "Вторник", "Wednesday": "Среда",
    "Thursday": "Четверг", "Friday": "Пятница", "Saturday": "Суббота",
    "Sunday": "Воскресенье",
}
_RU_MONTHS = {
    "January": "января", "February": "февраля", "March": "марта",
    "April": "апреля", "May": "мая", "June": "июня", "July": "июля",
    "August": "августа", "September": "сентября", "October": "октября",
    "November": "ноября", "December": "декабря",
}

def _is_russian(text: str) -> bool:
    return any("Ѐ" <= ch <= "ӿ" for ch in text)

def _localize_day(day_label: str, lang: str) -> str:
    if lang != "ru":
        return day_label
    m = re.match(r"(\w+),\s+(\w+)\s+(\d+)", day_label)
    if not m:
        return day_label
    dow, month, num = m.group(1), m.group(2), m.group(3)
    return f"{_RU_DAYS.get(dow, dow)}, {num} {_RU_MONTHS.get(month, month)}"

def _localize_md(day_label: str, lang: str) -> str:
    m = re.match(r"(\w+),\s+(\w+)\s+(\d+)", day_label)
    if not m:
        return day_label
    _dow, month, num = m.group(1), m.group(2), m.group(3)
    if lang == "ru":
        return f"{num} {_RU_MONTHS.get(month, month)}"
    return f"{month} {num}"

def _format_span_line(e: dict, lang: str) -> str:
    title    = e["title"]
    start_md = _localize_md(e["day"], lang)
    if e.get("multi_day"):
        end_md = _localize_md(e.get("end_day", e["day"]), lang)
        if e.get("all_day"):
            tag = "весь день" if lang == "ru" else "all day"
            return f"🗓 {title} — {start_md} → {end_md} ({tag})"
        st, et = e.get("time", ""), e.get("end_time", "")
        return f"🗓 {title} — {start_md} {st} → {end_md} {et}"
    tag = "весь день" if lang == "ru" else "all day"
    return f"🗓 {title} — {start_md} ({tag})"

def _format_calendar_result(events: list[dict], lang: str = "en") -> str:
    if not events:
        return ("В вашем календаре нет событий на указанный период."
                if lang == "ru"
                else "There are no events in your calendar for the requested period.")

    spanning = [e for e in events if e.get("multi_day") or e.get("all_day")]
    timed    = [e for e in events if not (e.get("multi_day") or e.get("all_day"))]
    lines: list[str] = []

    for e in spanning:
        lines.append(_format_span_line(e, lang))

    current_day = None
    for e in timed:
        if e["day"] != current_day:
            if lines:
                lines.append("")
            lines.append(f"📅 {_localize_day(e['day'], lang)}")
            current_day = e["day"]
        t = e.get("time", "")
        lines.append(f"   {t}  {e['title']}" if t else f"   {e['title']}")
    return "\n".join(lines)


# ── Tasks formatter ───────────────────────────────────────────────────────────

def _format_tasks_result(tasks: list[dict], lang: str = "en") -> str:
    if not tasks:
        return ("Открытых задач нет." if lang == "ru" else "You have no open tasks.")

    by_list: dict[str, list[str]] = {}
    for t in tasks:
        lst   = t.get("list", "Tasks")
        title = t.get("title", "(untitled)")
        due   = t.get("due")
        entry = title
        if due:
            try:
                from datetime import date as _date
                due_d = _date.fromisoformat(due[:10])
                if lang == "ru":
                    m = _RU_MONTHS.get(due_d.strftime("%B"), due_d.strftime("%B"))
                    entry = f"{title}  (до {due_d.day} {m})"
                else:
                    entry = f"{title}  (due {due_d.strftime('%B')} {due_d.day})"
            except ValueError:
                pass
        by_list.setdefault(lst, []).append(entry)

    lines: list[str] = []
    for lst_name, titles in by_list.items():
        if lines:
            lines.append("")
        lines.append(f"📋 {lst_name}")
        for t in titles:
            lines.append(f"   • {t}")
    return "\n".join(lines)


# ── Proactive task formatter (briefings) ─────────────────────────────────────
# Used only by build_briefing — groups tasks by urgency, surfaces overdue items.

def _format_tasks_result_proactive(tasks: list[dict], lang: str = "en") -> str:
    """
    Smarter task formatter for briefings:
    - Overdue tasks flagged at the top
    - Due today shown next
    - Upcoming tasks grouped below
    - Empty state with a nudge if there's an overdue item elsewhere
    """
    if not tasks:
        return ("Открытых задач нет." if lang == "ru" else "No open tasks.")

    today = date.today()

    overdue:  list[tuple[str, str]] = []   # (list_name, entry_line)
    due_today: list[tuple[str, str]] = []
    upcoming:  list[tuple[str, str]] = []
    no_date:   list[tuple[str, str]] = []

    for t in tasks:
        lst   = t.get("list", "Tasks")
        title = t.get("title", "(untitled)")
        due   = t.get("due")
        if due:
            try:
                due_d = date.fromisoformat(due[:10])
                delta = (due_d - today).days
                if delta < 0:
                    days_over = -delta
                    suffix = f"overdue {days_over}d" if lang == "en" else f"просрочено {days_over}д"
                    overdue.append((lst, f"{title}  ⚠️ ({suffix})"))
                elif delta == 0:
                    suffix = "due today" if lang == "en" else "сегодня"
                    due_today.append((lst, f"{title}  ({suffix})"))
                else:
                    if lang == "ru":
                        m = _RU_MONTHS.get(due_d.strftime("%B"), due_d.strftime("%B"))
                        suffix = f"до {due_d.day} {m}"
                    else:
                        suffix = f"due {due_d.strftime('%b')} {due_d.day}"
                    upcoming.append((lst, f"{title}  ({suffix})"))
            except ValueError:
                no_date.append((lst, title))
        else:
            no_date.append((lst, title))

    lines: list[str] = []

    def _section(header: str, items: list[tuple[str, str]]) -> None:
        if not items:
            return
        if lines:
            lines.append("")
        lines.append(header)
        # Group within section by list
        by_list: dict[str, list[str]] = {}
        for lst, entry in items:
            by_list.setdefault(lst, []).append(entry)
        for lst_name, entries in by_list.items():
            lines.append(f"  📋 {lst_name}")
            for e in entries:
                lines.append(f"     • {e}")

    if overdue:
        hdr = (f"🔴 Overdue ({len(overdue)} task{'s' if len(overdue)!=1 else ''})"
               if lang == "en"
               else f"🔴 Просрочено ({len(overdue)})")
        _section(hdr, overdue)

    if due_today:
        hdr = "🟡 Due today" if lang == "en" else "🟡 На сегодня"
        _section(hdr, due_today)

    if upcoming:
        hdr = "📌 Upcoming" if lang == "en" else "📌 Предстоящее"
        _section(hdr, upcoming)

    if no_date:
        hdr = "📝 No due date" if lang == "en" else "📝 Без срока"
        _section(hdr, no_date)

    return "\n".join(lines)


# ── Delete preview formatter ──────────────────────────────────────────────────
# Python-formatted preview shown before any calendar delete executes.
# The model never authors this content.

def _format_delete_preview(result: dict, lang: str) -> str:
    matches = result.get("matches", [])
    count   = result.get("count", 0)
    if count == 0:
        return ("Совпадающих событий не найдено." if lang == "ru"
                else "No matching events found.")
    lines = [("⚠️ Будет удалено:" if lang == "ru" else "⚠️ About to delete:")]
    for m in matches:
        when = m.get("when", "")[:16].replace("T", "  ")
        lines.append(f"   • {m['title']}  ({when})")
    lines.append("")
    lines.append("Подтвердить? (да / нет)" if lang == "ru"
                 else "Confirm deletion? (yes / no)")
    return "\n".join(lines)


# ── Listing short-circuit ─────────────────────────────────────────────────────

LISTING_TOOLS = {"list_calendar_events", "list_tasks"}

_CAL_INTRO  = {"ru": "Вот ваш календарь:", "en": "Here is your calendar:"}
_TASK_INTRO = {"ru": "Вот ваши открытые задачи:", "en": "Here are your open tasks:"}

def _python_format_listing(fn_name: str, result: list, lang: str) -> str:
    if fn_name == "list_calendar_events":
        return f"{_CAL_INTRO[lang]}\n\n{_format_calendar_result(result, lang)}"
    if fn_name == "list_tasks":
        return f"{_TASK_INTRO[lang]}\n\n{_format_tasks_result(result, lang)}"
    return json.dumps(result, default=str)


# ── List-query detector ───────────────────────────────────────────────────────
# Broad intentionally — any false positive just forces a harmless tool call.
# A false NEGATIVE lets the model answer without tools → fabrication risk.

_LIST_QUERY_RE = re.compile(
    # Task keywords
    r'task|задач|задани|todo|to-do|'
    # Calendar keywords
    r'calendar|event|agenda|schedule|appointment|'
    r'расписан|календар|событи|встреч|'
    # List/show phrases that appear without the above nouns
    r'what.s on|what do i have|what.s my|show me|show my|'
    r'что (у меня|на|запланировано|стоит|есть)|покаж|список|'
    # "my list" / "my tasks" / "my schedule" — catches "what's on my list"
    r'my list|my tasks|my events|my schedule',
    re.IGNORECASE | re.UNICODE,
)

def _is_list_query(text: str) -> bool:
    """True when the message is asking to list tasks or calendar data."""
    return bool(_LIST_QUERY_RE.search(text))


# ── Conversational-turn detector ──────────────────────────────────────────────
# Extremely narrow: only single-word confirmations/acknowledgments.
# Everything else is treated as potentially needing a tool call.

_CONVERSATIONAL_RE = re.compile(
    r'^(yes|no|да|нет|ok|ок|okay|sure|thanks?|thank\s+you|'
    r'спасибо|понятно|ясно|хорошо|отлично|[👍✓✅])\s*[!.]*$',
    re.IGNORECASE | re.UNICODE,
)

def _is_conversational(text: str) -> bool:
    """True only for bare acknowledgments that need no data lookup."""
    return bool(_CONVERSATIONAL_RE.match(text.strip()))


# ── Argument parser ───────────────────────────────────────────────────────────

def _parse_tool_args(raw_args) -> dict:
    if isinstance(raw_args, dict):
        return raw_args
    try:
        return json.loads(raw_args)
    except (json.JSONDecodeError, TypeError):
        pass
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_args, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    match = re.search(r"\{.*\}", raw_args, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    raise ValueError(f"Could not parse tool arguments: {raw_args!r}")


# ── Tool-invocation failure detector ─────────────────────────────────────────

_TOOL_ATTEMPT_RE = re.compile(
    r'list_calendar_events|list_tasks|create_calendar_event|set_event_reminder|'
    r'delete_calendar_event|create_task|update_task|complete_task|save_memory|ask_clarification|web_search|summarize_unread|search_email|'
    r'\bdate_str\b|\bend_date\b|\btask_title\b|\blist_name\b|\bdue_datetime\b|'
    r'\breminder_minutes\b|CalendarEvent|CalendarEvents',
    re.IGNORECASE,
)

def _looks_like_tool_attempt(text: str) -> bool:
    return bool(text and _TOOL_ATTEMPT_RE.search(text))


# ── Core agent loop ───────────────────────────────────────────────────────────

def run_agent(user_message: str, history: list | None = None,
              chat_id: int | None = None) -> str:
    """
    Send a user message through the tool-calling loop and return the final answer.

    Args:
        user_message: Natural-language request from the user.
        history:      Prior conversation turns (list of message dicts).
        chat_id:      Telegram chat ID — used for the confirmation rail on deletes.
    """
    # Build chat_id-aware set_reminder and inject it for this call
    def _set_reminder_for_chat(message: str, fire_at: str) -> dict:
        if chat_id is None:
            return {"error": "Cannot set reminder — no chat_id available."}
        try:
            rid = _reminders.add(chat_id, message, fire_at)
            return {"status": "reminder_set", "id": rid, "message": message, "fire_at": fire_at}
        except ValueError as exc:
            return {"error": str(exc)}

    # Temporarily override the stub for this run
    TOOL_FUNCTIONS["set_reminder"] = _set_reminder_for_chat

    _d = date.today()
    today_str = f"{_d.strftime('%A, %B')} {_d.day}, {_d.year}"

    # Inject relevant memories from persistent store
    relevant = _memory.search_relevant(user_message, limit=6)
    memory_block = (
        "\n\n" + _memory.format_for_prompt(relevant)
        if relevant else ""
    )

    system = {
        "role": "system",
        "content": (
            f"You are a helpful personal assistant. Today is {today_str}. "
            "You have access to the user's Google Calendar and Google Tasks via tools.\n\n"

            "TOOL USE: Always call the appropriate tool to fetch or modify real data. "
            "Never invent, guess, or fabricate events, tasks, or confirmations. "
            "For ANY request to add/create/schedule/remind/update/complete — call the tool immediately.\n\n"

            "TRUTHFULNESS: Report ONLY what a tool actually returned. If a tool result contains "
            "an 'error' field, surface that error to the user. Never paper over errors with "
            "made-up data. Never claim an action succeeded unless the tool confirmed it.\n\n"

            "ROUTING — TASK vs CALENDAR EVENT (follow strictly):\n"
            "Use these signals to decide which tool to call for a creation request:\n"
            "  → CALENDAR EVENT (create_calendar_event) when ANY of:\n"
            "     • User says 'event', 'meeting', 'appointment', 'session', 'call' (scheduled), "
            "'book', 'block time', 'doctor', 'dentist'\n"
            "     • User says 'remind me' with a specific time\n"
            "     • Request has BOTH a specific time AND a reminder ('remind me 30 min before')\n"
            "  → TASK (create_task) when ANY of:\n"
            "     • User says 'task', 'to-do', 'todo', 'add to my list', 'buy', 'submit', "
            "'finish', 'write', 'complete'\n"
            "     • Request has ONLY a due date, no specific time, no reminder\n"
            "  → ASK (ask_clarification) when ALL of:\n"
            "     • None of the clear signals above are present\n"
            "     • A specific time is mentioned\n"
            "     • It could reasonably be either a calendar event or a task\n"
            "     Example: 'Call John tomorrow at 3pm' with no 'event'/'task'/'remind' keyword.\n"
            "  NEVER guess silently when ambiguous — always ask.\n"
            "  If the user already told you which they want (e.g. after an ask_clarification), "
            "honour that choice immediately.\n\n"

            "TASKS — ADDITIONAL RULES:\n"
            "• The user's task lists are determined by their Google account (fetched live).\n"
            "• When the user says 'My Tasks', 'my list', or doesn't specify a list → omit list_name "
            "(use the primary list).\n"
            "• NEVER invent a list name. If the user mentions a list that doesn't exist, tell them "
            "the available lists and ask which one to use.\n"
            "• Tasks API CANNOT store a time. When the user specifies a time for a task "
            "(e.g. 'task tomorrow at 15:00'), use due_datetime — this sets the task due date "
            "to that day AND creates a Calendar event at that time. Report both to the user.\n"
            "• For date-only tasks (no time mentioned), use due_date.\n"
            "• To update an existing task's date/time/title, call update_task, not create_task.\n\n"

            "CALENDAR REMINDERS:\n"
            "• When creating an event: if the user says 'remind me X before', set reminder_minutes "
            "in the create_calendar_event call. Convert: '30 min before'→30, '1 hour'→60, "
            "'1 day before'→1440, 'at event time'→0.\n"
            "• To add/change a reminder on an EXISTING event, call set_event_reminder.\n\n"

            "CALENDAR COLORS:\n"
            "• When creating an event with a color ('make it red', 'in blue'), pass color= to "
            "create_calendar_event. Accepts words like 'red', 'blue', 'green', 'yellow', 'orange', "
            "'purple', 'pink', 'teal', 'gray' or Google names like 'Tomato', 'Blueberry'.\n"
            "• To change the color of an EXISTING event, call update_event_color.\n"
            "• If the color is unrecognised, the tool returns an error with available options — "
            "relay that list to the user.\n\n"

            "DESTRUCTIVE ACTIONS (delete): Two-step. Call delete WITHOUT confirm first to preview "
            "— nothing is deleted. Show preview, ask to confirm. Only then call with confirm=true.\n\n"

            "EMAIL: For any request about inbox, unread emails, or finding emails, call "
            "summarize_unread or search_email. Report ONLY what the tool returns — sender, "
            "subject, date, snippet. Flag urgent items (is_urgent=true) prominently. "
            "Never summarize or paraphrase beyond the snippet the tool provides.\n\n"

            "WEB SEARCH: For any question requiring current events, facts, definitions, prices, "
            "news, or information not in the user's calendar/tasks, call web_search. "
            "Never answer from training data alone for time-sensitive questions. "
            "Always cite the source URLs from the results.\n\n"

            "MEMORY: When the user says 'remember', 'note that', 'always X', or 'my preference is', "
            "call save_memory with the fact. Never store secrets or passwords.\n\n"

            "LOGGING: When the user mentions eating, drinking, or food — call log_calories. "
            "When they mention a habit or activity (gym, run, water, reading, etc.) — call log_habit. "
            "When they ask what they ate or logged — call get_log. "
            "NEVER compute targets, deficits, macros, or offer any diet/nutrition advice. "
            "NEVER evaluate or comment on quantities. Just confirm what was logged.\n\n"

            "REMINDERS: When the user asks to be reminded about something at a specific time or after "
            "a delay, call set_reminder. Convert relative times ('in 2 hours', 'tomorrow at 9am') to "
            "absolute ISO 8601 datetimes using today's date and the user's local timezone. "
            "Confirm back with the exact time you scheduled.\n\n"

            "LANGUAGE: Reply in the language the user wrote in. Russian → Russian. English → English."
            + memory_block
        ),
    }

    messages = [system] + (history or []) + [{"role": "user", "content": user_message}]

    MAX_TOOL_ROUNDS = 10
    lang = "ru" if _is_russian(user_message) else "en"

    for round_num in range(MAX_TOOL_ROUNDS):

        # ── tool_choice policy ────────────────────────────────────────────────
        # Round 0: "required" by default — the model must call at least one tool
        # before producing any output.  The only exception is a bare conversational
        # acknowledgment ("yes", "thanks", "да") where forcing a tool call would
        # be confusing and there is nothing to fabricate.
        # All subsequent rounds: "auto" — the model decides if another tool is needed.
        if round_num == 0 and not _is_conversational(user_message):
            tool_choice = "required"
            print(f"  [round 0] tool_choice=required  "
                  f"(list_query={_is_list_query(user_message)})")
        else:
            tool_choice = "auto"

        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice=tool_choice,
        )

        assistant_msg = response.choices[0].message

        # ── Retry guard ───────────────────────────────────────────────────────
        # Small models occasionally print a tool call as plain text instead of
        # using the API mechanism.  Retry once with tool_choice="required".
        if not assistant_msg.tool_calls and _looks_like_tool_attempt(
                assistant_msg.content or ""):
            bad_content = (assistant_msg.content or "")[:120]
            print(f"  [RETRY round {round_num+1}] Model echoed tool-call as text "
                  f"({bad_content!r}); retrying with tool_choice='required'")
            try:
                retry_resp = client.chat.completions.create(
                    model=MODEL,
                    messages=messages,
                    tools=TOOLS,
                    tool_choice="required",
                )
                retry_msg = retry_resp.choices[0].message
            except Exception as exc:
                print(f"  [RETRY FAILED] API error: {exc}")
                retry_msg = None

            if retry_msg and retry_msg.tool_calls:
                print(f"  [RETRY round {round_num+1}] Succeeded — "
                      f"{len(retry_msg.tool_calls)} tool call(s) received")
                assistant_msg = retry_msg
            else:
                print(f"  [RETRY FAILED round {round_num+1}] Still no tool calls after retry")
                return ("Не удалось обработать запрос — попробуйте переформулировать."
                        if lang == "ru"
                        else "I couldn't process that request — please try rephrasing.")

        # ── Backstop: Ollama ignored tool_choice="required" ──────────────────
        # If round 0 produced no tool calls despite tool_choice="required",
        # Ollama silently ignored the constraint (known qwen2.5:7b behaviour).
        # The model's free-form content may be fabricated — never return it for
        # data queries.  Retry once; if that also yields no calls, return a
        # safe error.  Conversational turns are exempt (they didn't force tools).
        if (round_num == 0
                and not _is_conversational(user_message)
                and not assistant_msg.tool_calls):
            bad_snippet = (assistant_msg.content or "")[:120]
            print(f"  [BACKSTOP round 0] tool_choice=required was ignored by Ollama "
                  f"— no tool calls in response ({bad_snippet!r}). Retrying.")
            try:
                bs_resp = client.chat.completions.create(
                    model=MODEL,
                    messages=messages,   # without appending yet
                    tools=TOOLS,
                    tool_choice="required",
                )
                bs_msg = bs_resp.choices[0].message
            except Exception as exc:
                print(f"  [BACKSTOP] API error on retry: {exc}")
                bs_msg = None

            if bs_msg and bs_msg.tool_calls:
                print(f"  [BACKSTOP] Retry succeeded — "
                      f"{len(bs_msg.tool_calls)} tool call(s)")
                assistant_msg = bs_msg
            else:
                # Both attempts produced no tool calls on a non-conversational
                # message.  Refuse to surface any model content — it may be
                # fabricated.  Return an honest error instead.
                print(f"  [BACKSTOP] Both attempts produced no tool calls — "
                      f"refusing free-form content, returning safe error")
                return ("Не удалось получить данные — попробуйте ещё раз."
                        if lang == "ru"
                        else "Couldn't fetch your data — please try again.")

        messages.append(assistant_msg)

        # No tool calls → model gave a final conversational answer.
        if not assistant_msg.tool_calls:
            final = assistant_msg.content or "(no response)"
            print("  " + "═" * 70)
            print(f"  [final reply to user]:\n{final}")
            print("  " + "═" * 70)
            return final

        # ── Execute every tool the model requested ────────────────────────────
        executed: list[tuple] = []   # (fn_name, args, result, tool_call_id)

        for tool_call in assistant_msg.tool_calls:
            fn_name  = tool_call.function.name
            raw_args = tool_call.function.arguments
            args: dict = {}

            try:
                args = _parse_tool_args(raw_args)
            except ValueError as exc:
                result = {"error": f"Argument parse failed: {exc}"}
            else:
                # ── Confirmation rail ─────────────────────────────────────────
                # delete_calendar_event(confirm=True) is only allowed if a
                # preview was already shown (pending entry exists for this chat).
                # Without a prior preview, we force confirm=False so the user
                # always sees what will be deleted before it happens.
                if fn_name == "delete_calendar_event" and args.get("confirm"):
                    pending = _pending_deletes.get(chat_id) if chat_id is not None else None
                    if pending is None:
                        print(f"  [CONFIRM-RAIL] confirm=True with no pending preview "
                              f"(chat={chat_id}) — intercepting, forcing preview first")
                        args = {**args, "confirm": False}
                    else:
                        print(f"  [CONFIRM-RAIL] Pending preview verified "
                              f"(chat={chat_id}, title={pending.get('title')!r}) "
                              f"— allowing delete, clearing pending")
                        _pending_deletes.pop(chat_id, None)

                fn = TOOL_FUNCTIONS.get(fn_name)
                if fn is None:
                    result = {"error": f"Unknown tool '{fn_name}'"}
                else:
                    try:
                        result = fn(**args)
                    except TypeError as exc:
                        result = {"error": f"Bad arguments for {fn_name}: {exc}"}
                    except Exception as exc:
                        result = {"error": str(exc)}

            print("  " + "─" * 70)
            print(f"  [tool round {round_num+1}] CALL : {fn_name}({raw_args})")
            print(f"  [tool round {round_num+1}] RAW  : {result!r}")

            executed.append((fn_name, args, result, tool_call.id))

        # ── Delete preview short-circuit ──────────────────────────────────────
        # When any executed tool returned a delete preview (needs_confirmation),
        # format it in Python, store the pending state, and return immediately.
        # The model never gets to author or modify this content.
        for fn_name, args, result, _ in executed:
            if (fn_name == "delete_calendar_event"
                    and isinstance(result, dict)
                    and result.get("needs_confirmation")):
                if chat_id is not None:
                    _pending_deletes[chat_id] = {
                        "title":    args.get("title", ""),
                        "date_str": args.get("date_str", ""),
                        "end_date": args.get("end_date"),
                    }
                    print(f"  [CONFIRM-RAIL] Stored pending delete "
                          f"(chat={chat_id}, title={args.get('title')!r})")
                preview = _format_delete_preview(result, lang)
                print("  " + "═" * 70)
                print(f"  [DELETE PREVIEW → user]:\n{preview}")
                print("  " + "═" * 70)
                return preview

        # ── Clarification short-circuit ───────────────────────────────────────
        # When the model calls ask_clarification, return the question directly.
        # No second LLM turn needed — the question is the response.
        for fn_name, _args, result, _ in executed:
            if (fn_name == "ask_clarification"
                    and isinstance(result, dict)
                    and result.get("clarification_needed")):
                q = result["question"]
                print("  " + "═" * 70)
                print(f"  [CLARIFICATION → user]:\n{q}")
                print("  " + "═" * 70)
                return q

        # ── Listing short-circuit ─────────────────────────────────────────────
        # When every call in this round is a listing tool AND every call
        # returned list data (no errors), Python formats the reply directly.
        # The model gets no second turn — titles come from code, never the model.
        all_listing = all(fn in LISTING_TOOLS for fn, _, _, _ in executed)
        all_success = all(isinstance(res, list) for _, _, res, _ in executed)

        # Only short-circuit when the user actually asked for a list.
        # Without this gate, a listing tool forced on a non-list query (e.g.
        # "yes" or "thanks") would surface an unsolicited task/event list.
        if all_listing and all_success and _is_list_query(user_message):
            parts = [_python_format_listing(fn, res, lang)
                     for fn, _, res, _ in executed]
            final = "\n\n".join(parts)
            print("  " + "═" * 70)
            print(f"  [SHORT-CIRCUIT Python reply]:\n{final}")
            print("  " + "═" * 70)
            # Populate button side-channel
            if chat_id is not None:
                all_tasks  = [r for fn, _, r, _ in executed if fn == "list_tasks"  for r in r]  # type: ignore[union-attr]
                all_events = [r for fn, _, r, _ in executed if fn == "list_calendar_events" for r in r]  # type: ignore[union-attr]
                # flatten correctly
                task_rows  = []
                event_rows = []
                for fn, _, res, _ in executed:
                    if fn == "list_tasks":
                        task_rows.extend(res)
                    elif fn == "list_calendar_events":
                        event_rows.extend([e for e in res if not e.get("all_day")])
                if task_rows:
                    _button_payloads[chat_id] = {"type": "tasks", "data": task_rows}
                elif event_rows:
                    _button_payloads[chat_id] = {"type": "events", "data": event_rows}
            return final

        # ── Creation side-channel: populate undo button payloads ─────────────
        if chat_id is not None:
            for fn_name, _a, result, _ in executed:
                if fn_name == "create_task" and isinstance(result, dict) and "id" in result:
                    _button_payloads[chat_id] = {
                        "type": "created_task",
                        "data": result,
                    }
                elif fn_name == "create_calendar_event" and isinstance(result, dict) and "id" in result:
                    _button_payloads[chat_id] = {
                        "type": "created_event",
                        "data": result,
                    }

        # ── Fall-through: non-listing or error — let the model respond ─────────
        for fn_name, args, result, tc_id in executed:
            content = json.dumps(result, default=str)
            print(f"  [tool round {round_num+1}] →MODEL:\n{content}")
            print("  " + "─" * 70)
            messages.append({
                "role":         "tool",
                "tool_call_id": tc_id,
                "content":      content,
            })

    return "(Agent hit the tool-call limit without a final answer — try a simpler request.)"


# ── Briefing builder ──────────────────────────────────────────────────────────
# Called directly by the scheduler — no LLM involved, no fabrication possible.
# Uses the same tool functions and formatters as the normal agent path.

# ── Reminder helpers for briefings ───────────────────────────────────────────

import datetime as _dt

def _reminders_for_date(target_date: "date", chat_id: int | None = None) -> list[dict]:
    """Return pending reminders whose local fire time falls on target_date."""
    local_tz = _dt.datetime.now().astimezone().tzinfo
    try:
        pending = (
            _reminders.get_pending_for_chat(chat_id)
            if chat_id is not None
            else _reminders.get_pending()
        )
    except Exception:
        return []
    result = []
    for r in pending:
        try:
            fire_utc   = _dt.datetime.fromisoformat(r["fire_at"]).replace(
                            tzinfo=_dt.timezone.utc)
            fire_local = fire_utc.astimezone(local_tz)
            if fire_local.date() == target_date:
                result.append({**r, "_local_time": fire_local.strftime("%H:%M")})
        except Exception:
            pass
    return result


def _fmt_reminders_section(reminders: list[dict], label: str = "⏰ Reminders") -> str:
    if not reminders:
        return f"{label}\n  None"
    lines = [label]
    for r in reminders:
        t = r.get("_local_time", "")
        msg = r.get("message", "")
        lines.append(f"  • {t}  {msg}" if t else f"  • {msg}")
    return "\n".join(lines)


def _fmt_events_section(events: list[dict], label: str = "📅 Events") -> str:
    if not events:
        return f"{label}\n  None scheduled"
    lines = [label]
    for ev in events:
        start = ev.get("start", "")
        end   = ev.get("end", "")
        time_str = f"{start}–{end}" if start and end else start
        lines.append(f"  • {ev.get('title', '?')}  {time_str}".rstrip())
    return "\n".join(lines)


# ── Morning briefing (09:00 — "orient me for the day") ───────────────────────

def build_briefing(date_keyword: str, lang: str = "en",
                   chat_id: int | None = None) -> str:
    """
    Morning briefing for date_keyword ("today" | "tomorrow").
    Each section is isolated — a failure in one never blocks the rest.
    """
    today = date.today()
    target_date = today if date_keyword == "today" else today + _dt.timedelta(days=1)

    parts: list[str] = []
    errors: list[str] = []

    # ── Weather ───────────────────────────────────────────────────────────────
    if date_keyword == "today":
        try:
            parts.append(_weather.get_weather(include_tomorrow=False))
        except Exception as exc:
            errors.append(f"weather: {type(exc).__name__}")
            parts.append("🌤 Weather\n  ⚠️ Could not load")

    # ── Calendar events ───────────────────────────────────────────────────────
    try:
        events = list_calendar_events(date_keyword)
        parts.append(_fmt_events_section(events, "📅 Today's events"
                                         if date_keyword == "today"
                                         else "📅 Tomorrow's events"))
    except Exception as exc:
        errors.append(f"calendar: {type(exc).__name__}")
        parts.append("📅 Events\n  ⚠️ Could not load")
        events = []

    # ── Tasks (with overdue surfaced) ─────────────────────────────────────────
    try:
        tasks = list_tasks()
        task_body = _format_tasks_result_proactive(tasks, lang)
        label = "✅ Today's tasks" if date_keyword == "today" else "✅ Tomorrow's tasks"
        parts.append(f"{label}\n\n{task_body}")
    except Exception as exc:
        errors.append(f"tasks: {type(exc).__name__}")
        parts.append("✅ Tasks\n  ⚠️ Could not load")
        tasks = []

    # ── Reminders for the day ─────────────────────────────────────────────────
    try:
        todays_reminders = _reminders_for_date(target_date, chat_id)
        label = "⏰ Today's reminders" if date_keyword == "today" else "⏰ Tomorrow's reminders"
        parts.append(_fmt_reminders_section(todays_reminders, label))
    except Exception as exc:
        errors.append(f"reminders: {type(exc).__name__}")
        parts.append("⏰ Reminders\n  ⚠️ Could not load")

    # ── Empty-day nudge ───────────────────────────────────────────────────────
    if date_keyword == "today" and not events:
        overdue_count = sum(
            1 for t in tasks
            if t.get("due") and date.fromisoformat(t["due"][:10]) < today
        )
        if overdue_count:
            parts.append(
                f"💡 Nothing scheduled — good day to tackle "
                f"{overdue_count} overdue task{'s' if overdue_count != 1 else ''}."
            )

    if errors:
        parts.append(f"⚠️ Partial load — failed sections: {', '.join(errors)}")

    return "\n\n".join(parts)


# ── Evening briefing (23:00 — "wrap up + prep tomorrow") ─────────────────────

def build_evening_briefing(chat_id: int | None = None) -> str:
    """
    Evening briefing: today's accomplishments + full tomorrow preview.
    Each section is isolated — failures are noted, not fatal.
    """
    from tools import list_completed_today

    today    = date.today()
    tomorrow = today + _dt.timedelta(days=1)
    header   = f"🌙 Evening briefing — {today.strftime('%A, %B')} {today.day}"

    parts: list[str] = [header]
    errors: list[str] = []

    # ── 1. Today's accomplishments ────────────────────────────────────────────
    try:
        done = list_completed_today()
    except Exception as exc:
        done = None
        errors.append(f"completed tasks: {type(exc).__name__}")

    try:
        past_events = list_calendar_events("today")
    except Exception as exc:
        past_events = None
        errors.append(f"today's events: {type(exc).__name__}")

    acc_lines = ["✅ Today's accomplishments"]
    if done is None:
        acc_lines.append("  ⚠️ Could not load completed tasks")
    elif done:
        for t in done:
            acc_lines.append(f"  • {t['title']}  ({t['list']})")
    else:
        acc_lines.append("  No tasks marked complete today")

    if past_events is None:
        acc_lines.append("  ⚠️ Could not load today's events")
    elif past_events:
        for ev in past_events:
            start    = ev.get("start", "")
            end      = ev.get("end", "")
            time_str = f"{start}–{end}" if start and end else start
            acc_lines.append(f"  📅 {ev.get('title', '?')}  {time_str}".rstrip())
    parts.append("\n".join(acc_lines))

    # ── 2. Tomorrow's events ──────────────────────────────────────────────────
    try:
        tmr_events = list_calendar_events("tomorrow")
        parts.append(_fmt_events_section(tmr_events, "📅 Tomorrow's events"))
    except Exception as exc:
        errors.append(f"tomorrow's events: {type(exc).__name__}")
        parts.append("📅 Tomorrow's events\n  ⚠️ Could not load")

    # ── 3. Tomorrow's tasks ───────────────────────────────────────────────────
    try:
        tmr_tasks = list_tasks()
        parts.append(f"✅ Tomorrow's tasks\n\n{_format_tasks_result_proactive(tmr_tasks)}")
    except Exception as exc:
        errors.append(f"tasks: {type(exc).__name__}")
        parts.append("✅ Tomorrow's tasks\n  ⚠️ Could not load")

    # ── 4. Tomorrow's reminders ───────────────────────────────────────────────
    try:
        tmr_reminders = _reminders_for_date(tomorrow, chat_id)
        parts.append(_fmt_reminders_section(tmr_reminders, "⏰ Tomorrow's reminders"))
    except Exception as exc:
        errors.append(f"reminders: {type(exc).__name__}")
        parts.append("⏰ Tomorrow's reminders\n  ⚠️ Could not load")

    # ── 5. Tomorrow's weather forecast ────────────────────────────────────────
    try:
        wx_block  = _weather.get_weather(include_tomorrow=True)
        tmr_lines = [l for l in wx_block.splitlines()
                     if "Tomorrow" in l or "tomorrow" in l]
        forecast  = "  ".join(tmr_lines) if tmr_lines else wx_block.splitlines()[-1]
        parts.append(f"🌤 Tomorrow's forecast\n  {forecast}")
    except Exception as exc:
        errors.append(f"weather: {type(exc).__name__}")
        parts.append("🌤 Tomorrow's forecast\n  ⚠️ Could not load")

    if errors:
        parts.append(f"⚠️ Partial load — failed sections: {', '.join(errors)}")

    return "\n\n".join(parts)


def build_digest() -> str:
    """
    Build the evening accomplishment digest for today.
    Synchronous — safe to call from a ThreadPoolExecutor.
    Kept for the /digest on-demand command.
    """
    from tools import list_completed_today, list_calendar_events

    today  = date.today()
    header = f"🌙 Evening digest — {today.strftime('%A, %B')} {today.day}"
    parts  = [header]

    # ── Completed tasks ───────────────────────────────────────────────────────
    try:
        done = list_completed_today()
    except Exception:
        done = []

    if done:
        lines = ["✅ Tasks completed today"]
        for t in done:
            lines.append(f"  • {t['title']}  ({t['list']})")
        parts.append("\n".join(lines))
    else:
        parts.append("✅ No tasks marked complete today")

    # ── Past events ───────────────────────────────────────────────────────────
    try:
        events = list_calendar_events("today")
    except Exception:
        events = []

    if events:
        lines = ["📅 Events today"]
        for ev in events:
            start = ev.get("start", "")
            end   = ev.get("end", "")
            time_str = f"{start}–{end}" if start and end else start
            lines.append(f"  • {ev.get('title', '?')}  {time_str}".rstrip())
        parts.append("\n".join(lines))
    else:
        parts.append("📅 No calendar events today")

    return "\n\n".join(parts)
