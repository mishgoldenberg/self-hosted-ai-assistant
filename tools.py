"""
tools.py — Google Calendar + Tasks API wrappers.

VERIFIED API BEHAVIOUR (tested against live account 2026-06-07):
  • Tasks API v1 `due` field is DATE-ONLY. Any time component sent (e.g.
    T15:00:00+03:00) is silently discarded; the API always stores T00:00:00.000Z.
    To give a task a visible time in Google Calendar we create a Calendar event
    alongside the task and report both.
  • Calendar reminders (overrides) round-trip perfectly via events.insert / patch.

Public API
──────────
  get_task_lists()
  list_calendar_events(date_str, end_date?)
  create_calendar_event(title, start, end, reminder_minutes?)
  set_event_reminder(title, date_str, reminder_minutes, end_date?)
  delete_calendar_event(title, date_str, end_date?, confirm?)
  list_tasks(list_name?)
  create_task(title, due_date?, due_datetime?, list_name?)
  update_task(task_title, list_name?, due_date?, due_datetime?, new_title?)
  complete_task(task_title, list_name?)
"""

import re
from datetime import datetime, date, timedelta, timezone
from auth import get_google_services

# ── Calendar color palette ────────────────────────────────────────────────────
# Verified against the Calendar API colors.get() endpoint (2026-06-08).
# colorId → (Google name, hex)
CALENDAR_COLORS: dict[str, tuple[str, str]] = {
    "1":  ("Lavender",  "#a4bdfc"),
    "2":  ("Sage",      "#7ae7bf"),
    "3":  ("Grape",     "#dbadff"),
    "4":  ("Flamingo",  "#ff887c"),
    "5":  ("Banana",    "#fbd75b"),
    "6":  ("Tangerine", "#ffb878"),
    "7":  ("Peacock",   "#46d6db"),
    "8":  ("Graphite",  "#e1e1e1"),
    "9":  ("Blueberry", "#5484ed"),
    "10": ("Basil",     "#51b749"),
    "11": ("Tomato",    "#dc2127"),
}

# Natural-language words → colorId
_COLOR_ALIASES: dict[str, str] = {
    # reds / pinks
    "red":       "11",  # Tomato
    "tomato":    "11",
    "pink":      "4",   # Flamingo
    "flamingo":  "4",
    "coral":     "4",
    # oranges
    "orange":    "6",   # Tangerine
    "tangerine": "6",
    # yellows
    "yellow":    "5",   # Banana
    "banana":    "5",
    # greens
    "green":     "10",  # Basil (darker, more visible)
    "basil":     "10",
    "sage":      "2",
    "mint":      "2",
    "lime":      "2",
    # teals / cyans
    "teal":      "7",   # Peacock
    "peacock":   "7",
    "cyan":      "7",
    "aqua":      "7",
    "turquoise": "7",
    # blues
    "blue":      "9",   # Blueberry
    "blueberry": "9",
    "navy":      "9",
    "indigo":    "9",
    "lavender":  "1",
    "periwinkle":"1",
    "light blue":"1",
    # purples
    "purple":    "3",   # Grape
    "grape":     "3",
    "violet":    "3",
    "mauve":     "3",
    # grays
    "gray":      "8",   # Graphite
    "grey":      "8",
    "graphite":  "8",
    "silver":    "8",
}


def resolve_color(word: str) -> tuple[str, str] | None:
    """
    Map a natural-language color word to (colorId, google_name).
    Returns None if not recognised.
    Case-insensitive; also accepts the Google name directly.
    """
    key = word.strip().lower()
    # Direct alias lookup
    if key in _COLOR_ALIASES:
        cid = _COLOR_ALIASES[key]
        return cid, CALENDAR_COLORS[cid][0]
    # Accept colorId directly ("1".."11")
    if key in CALENDAR_COLORS:
        return key, CALENDAR_COLORS[key][0]
    return None


def color_options_text() -> str:
    """Human-readable list of available color names for error messages."""
    names = [f"{name} ({hex_})" for _, (name, hex_) in CALENDAR_COLORS.items()]
    return "Available colors: " + ", ".join(names)


# ── Lazy service initialisation ───────────────────────────────────────────────

_calendar_svc = None
_tasks_svc    = None

def _get_services():
    global _calendar_svc, _tasks_svc
    if _calendar_svc is None:
        _calendar_svc, _tasks_svc = get_google_services()
    return _calendar_svc, _tasks_svc


# ══════════════════════════════════════════════════════════════════════════════
# HELPER — timezone
# ══════════════════════════════════════════════════════════════════════════════

def _local_tz():
    """Return the system's local tzinfo (works on Windows without zoneinfo)."""
    return datetime.now(timezone.utc).astimezone().tzinfo


def _tz_offset_str() -> str:
    """Return the local UTC offset as '+HH:MM' string, e.g. '+03:00'."""
    raw = datetime.now(timezone.utc).astimezone().strftime("%z")  # "+0300"
    return f"{raw[:3]}:{raw[3:]}"                                  # "+03:00"


def _add_tz(dt_str: str) -> str:
    """Append local timezone offset to a naive ISO datetime string."""
    if "+" in dt_str or dt_str.endswith("Z"):
        return dt_str
    return dt_str + _tz_offset_str()


# ══════════════════════════════════════════════════════════════════════════════
# HELPER — date-range resolver
# ══════════════════════════════════════════════════════════════════════════════

_DATE_KEYWORD_MAP: dict[str, str] = {
    "today":                        "today",
    "tomorrow":                     "tomorrow",
    "this week":                    "this week",
    "next week":                    "next week",
    "week after next":              "week after next",
    "сегодня":                      "today",
    "завтра":                       "tomorrow",
    "эта неделя":                   "this week",
    "эту неделю":                   "this week",
    "на этой неделе":               "this week",
    "следующая неделя":             "next week",
    "следующей неделе":             "next week",
    "на следующей неделе":          "next week",
    "через неделю":                 "next week",
    "неделя после следующей":       "week after next",
    "неделю после следующей":       "week after next",
    "через две недели":             "week after next",
    "через 2 недели":               "week after next",
}

_RANGE_PATTERNS = [
    r'с\s+(\d{1,2}[./]\d{1,2})\s+по\s+(\d{1,2}[./]\d{1,2})',
    r'между\s+(\d{1,2}[./]\d{1,2})\s+и\s+(\d{1,2}[./]\d{1,2})',
    r'from\s+(\d{1,2}[./]\d{1,2})\s+to\s+(\d{1,2}[./]\d{1,2})',
    r'(\d{1,2}[./]\d{1,2})\s*(?:to|-|–|до)\s*(\d{1,2}[./]\d{1,2})',
]


def _parse_ddmm(s: str) -> date | None:
    m = re.fullmatch(r'(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?', s.strip())
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    if m.group(3):
        year = int(m.group(3))
        if year < 100:
            year += 2000
    else:
        year = date.today().year
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_range_phrase(s: str) -> tuple[date, date] | None:
    s = s.strip()
    for pattern in _RANGE_PATTERNS:
        m = re.search(pattern, s, re.IGNORECASE)
        if m:
            start = _parse_ddmm(m.group(1))
            end   = _parse_ddmm(m.group(2))
            if start and end:
                return start, end
    return None


def _resolve_dates(date_str: str, end_date: str | None = None) -> tuple[date, date]:
    today = date.today()
    key   = date_str.lower().strip()

    phrase = _parse_range_phrase(key)
    if phrase:
        return phrase

    canonical  = _DATE_KEYWORD_MAP.get(key, key)
    is_keyword = canonical in ("today", "tomorrow", "this week",
                               "next week", "week after next")
    if is_keyword:
        if canonical == "today":
            return today, today
        if canonical == "tomorrow":
            d = today + timedelta(days=1); return d, d
        if canonical == "this week":
            s = today - timedelta(days=today.weekday())
            return s, s + timedelta(days=6)
        if canonical == "next week":
            s = today - timedelta(days=today.weekday()) + timedelta(weeks=1)
            return s, s + timedelta(days=6)
        if canonical == "week after next":
            s = today - timedelta(days=today.weekday()) + timedelta(weeks=2)
            return s, s + timedelta(days=6)

    if end_date:
        s = _parse_ddmm(date_str)  or date.fromisoformat(date_str)
        e = _parse_ddmm(end_date)  or date.fromisoformat(end_date)
        return s, e

    d = _parse_ddmm(date_str) or date.fromisoformat(date_str)
    return d, d


def _resolve_date_range(date_str: str, end_date: str | None = None) -> tuple[str, str]:
    tz = _local_tz()
    start, end = _resolve_dates(date_str, end_date)
    time_min = datetime(start.year, start.month, start.day,  0,  0,  0, tzinfo=tz).isoformat()
    time_max = datetime(end.year,   end.month,   end.day,   23, 59, 59, tzinfo=tz).isoformat()
    return time_min, time_max


# ══════════════════════════════════════════════════════════════════════════════
# HELPER — task-list name resolver
# ══════════════════════════════════════════════════════════════════════════════

# Any of these mean "use the primary / default task list".
_MY_TASKS_ALIASES = frozenset({
    "my tasks", "my task", "mytasks", "default", "@default",
    "мои задачи", "моя задача", "мои задания", "основные задачи",
    "задачи",
})


def get_task_lists() -> list[dict]:
    """Return all task lists as [{id, title}]. Primary list is always first."""
    _, tasks = _get_services()
    items = tasks.tasklists().list().execute().get("items", [])
    return [{"id": tl["id"], "title": tl["title"]} for tl in items]


def _resolve_list_name(list_name: str) -> tuple[str, str]:
    """
    Resolve a human-readable list name to (list_id, list_title).

    Special case: any 'My Tasks' alias in English or Russian → primary list.
    Otherwise: exact match, then substring match.
    Raises ValueError with available list names if nothing matches.
    """
    all_lists = get_task_lists()
    needle    = list_name.lower().strip()

    # "My Tasks" (and Russian/alias variants) always → primary list.
    if needle in _MY_TASKS_ALIASES:
        return all_lists[0]["id"], all_lists[0]["title"]

    # Exact match.
    for tl in all_lists:
        if tl["title"].lower() == needle:
            return tl["id"], tl["title"]

    # Substring match.
    matches = [tl for tl in all_lists if needle in tl["title"].lower()]
    if len(matches) == 1:
        return matches[0]["id"], matches[0]["title"]
    if len(matches) > 1:
        names = [m["title"] for m in matches]
        raise ValueError(
            f"Ambiguous list name '{list_name}' matches: {names}. Be more specific.")

    available = [tl["title"] for tl in all_lists]
    raise ValueError(
        f"No task list named '{list_name}'. "
        f"Available lists: {available}. "
        f"Use one of these exact names, or ask the user if they want to create a new list.")


def _default_list() -> tuple[str, str]:
    """Return (id, title) of the primary task list."""
    lists = get_task_lists()
    return lists[0]["id"], lists[0]["title"]


# ══════════════════════════════════════════════════════════════════════════════
# CALENDAR FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def list_calendar_events(date_str: str, end_date: str | None = None) -> list[dict]:
    """
    Return events for a date or date range, in local time.

    Args:
        date_str:  Keyword or ISO date "YYYY-MM-DD".
        end_date:  Optional range end "YYYY-MM-DD".

    Returns:
        List of {id, summary, start, end, ...} dicts, ordered by start time.
    """
    calendar, _ = _get_services()

    range_start, range_end = _resolve_dates(date_str, end_date)
    time_min = datetime(range_start.year, range_start.month, range_start.day,
                        0, 0, 0, tzinfo=_local_tz()).isoformat()
    time_max = datetime(range_end.year,   range_end.month,   range_end.day,
                        23, 59, 59, tzinfo=_local_tz()).isoformat()

    result = calendar.events().list(
        calendarId="primary",
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    events = []
    for e in result.get("items", []):
        raw_start  = e["start"].get("dateTime", e["start"].get("date"))
        raw_end    = e["end"].get("dateTime",   e["end"].get("date"))
        is_all_day = "dateTime" not in e["start"]

        try:
            start_date  = date.fromisoformat(raw_start[:10])
            end_date_d  = date.fromisoformat(raw_end[:10])
        except (ValueError, TypeError):
            continue

        last_day = end_date_d - timedelta(days=1) if is_all_day else end_date_d
        if last_day < start_date:
            last_day = start_date

        if last_day < range_start or start_date > range_end:
            continue

        multi_day   = last_day > start_date
        start_label = f"{start_date.strftime('%A, %B')} {start_date.day}"
        end_label   = f"{last_day.strftime('%A, %B')} {last_day.day}"
        start_time  = raw_start[11:16] if "T" in raw_start else ""
        end_time    = raw_end[11:16]   if "T" in raw_end   else ""

        events.append({
            "id":        e["id"],
            "day":       start_label,
            "time":      start_time,
            "title":     e.get("summary", "(no title)"),
            "all_day":   is_all_day,
            "multi_day": multi_day,
            "end_day":   end_label,
            "end_time":  end_time,
        })
    return events


def create_calendar_event(title: str, start: str, end: str,
                          reminder_minutes: int | None = None,
                          color: str | None = None) -> dict:
    """
    Create a timed event on the primary calendar.

    Args:
        title:            Event name.
        start:            ISO 8601 datetime, e.g. "2026-06-08T14:00:00".
        end:              ISO 8601 datetime, same format.
        reminder_minutes: Minutes before event for popup. None = calendar default.
        color:            Natural-language color word or Google color name (optional).
                          e.g. "red", "blue", "Tomato". Unrecognised → error dict.

    Returns:
        {id, summary, start, end, reminder_minutes, color_name?}
    """
    calendar, _ = _get_services()

    # Resolve color
    color_id: str | None = None
    color_name: str | None = None
    if color:
        resolved = resolve_color(color)
        if resolved is None:
            return {"error": f"Unknown color '{color}'. {color_options_text()}"}
        color_id, color_name = resolved

    body = {
        "summary": title,
        "start":   {"dateTime": _add_tz(start)},
        "end":     {"dateTime": _add_tz(end)},
    }
    if color_id:
        body["colorId"] = color_id

    if reminder_minutes is not None:
        body["reminders"] = {
            "useDefault": False,
            "overrides":  [{"method": "popup", "minutes": reminder_minutes}],
        }
    else:
        body["reminders"] = {"useDefault": True}

    created = calendar.events().insert(calendarId="primary", body=body).execute()

    stored_rem = created.get("reminders", {})
    stored_min: int | None = None
    if not stored_rem.get("useDefault", True):
        overrides = stored_rem.get("overrides", [])
        if overrides:
            stored_min = overrides[0].get("minutes")

    result = {
        "id":               created["id"],
        "summary":          created["summary"],
        "start":            created["start"].get("dateTime"),
        "end":              created["end"].get("dateTime"),
        "reminder_minutes": stored_min,
    }
    if color_name:
        result["color_name"] = color_name
    return result


def set_event_reminder(title: str, date_str: str,
                       reminder_minutes: int,
                       end_date: str | None = None) -> dict:
    """
    Find an existing event by title+date and set (or update) its popup reminder.

    Args:
        title:            Full or partial event title (case-insensitive).
        date_str:         Date or range start to search within.
        reminder_minutes: Minutes before event for the popup.
                          0 = at the time of the event, 1440 = 1 day before.
        end_date:         Optional range end "YYYY-MM-DD".

    Returns:
        {updated, title, start, reminder_minutes} on success, or {error}.
    """
    calendar, _ = _get_services()
    range_start, range_end = _resolve_dates(date_str, end_date)

    time_min = datetime(range_start.year, range_start.month, range_start.day,
                        0, 0, 0, tzinfo=_local_tz()).isoformat()
    time_max = datetime(range_end.year, range_end.month, range_end.day,
                        23, 59, 59, tzinfo=_local_tz()).isoformat()

    result = calendar.events().list(
        calendarId="primary", timeMin=time_min, timeMax=time_max,
        singleEvents=True, orderBy="startTime",
    ).execute()

    needle  = title.lower().strip()
    matches = [e for e in result.get("items", [])
               if needle in e.get("summary", "").lower()]

    if not matches:
        return {"error": f"No event matching '{title}' found between {range_start} and {range_end}."}

    if len(matches) > 1:
        candidates = [
            {"title": e.get("summary"),
             "start": e["start"].get("dateTime", e["start"].get("date"))}
            for e in matches
        ]
        return {
            "error": f"Multiple events match '{title}' — be more specific.",
            "candidates": candidates,
        }

    event   = matches[0]
    patched = calendar.events().patch(
        calendarId="primary",
        eventId=event["id"],
        body={"reminders": {
            "useDefault": False,
            "overrides":  [{"method": "popup", "minutes": reminder_minutes}],
        }},
    ).execute()

    raw_start = patched["start"].get("dateTime", patched["start"].get("date"))
    return {
        "updated":          True,
        "title":            patched.get("summary"),
        "start":            raw_start,
        "reminder_minutes": reminder_minutes,
    }


def update_event_color(title: str, date_str: str,
                       color: str,
                       end_date: str | None = None) -> dict:
    """
    Find an existing event by title+date and set its color.

    Args:
        title:    Full or partial event title (case-insensitive).
        date_str: Date or range start to search within ("YYYY-MM-DD" or keywords).
        color:    Natural-language color word or Google name (e.g. "red", "Tomato").
        end_date: Optional range end "YYYY-MM-DD".

    Returns:
        {updated, title, start, color_name} on success, or {error}.
    """
    resolved = resolve_color(color)
    if resolved is None:
        return {"error": f"Unknown color '{color}'. {color_options_text()}"}
    color_id, color_name = resolved

    calendar, _ = _get_services()
    range_start, range_end = _resolve_dates(date_str, end_date)

    time_min = datetime(range_start.year, range_start.month, range_start.day,
                        0, 0, 0, tzinfo=_local_tz()).isoformat()
    time_max = datetime(range_end.year, range_end.month, range_end.day,
                        23, 59, 59, tzinfo=_local_tz()).isoformat()

    result = calendar.events().list(
        calendarId="primary", timeMin=time_min, timeMax=time_max,
        singleEvents=True, orderBy="startTime",
    ).execute()

    needle  = title.lower().strip()
    matches = [e for e in result.get("items", [])
               if needle in e.get("summary", "").lower()]

    if not matches:
        return {"error": f"No event matching '{title}' found between {range_start} and {range_end}."}

    if len(matches) > 1:
        candidates = [
            {"title": e.get("summary"),
             "start": e["start"].get("dateTime", e["start"].get("date"))}
            for e in matches
        ]
        return {
            "error": f"Multiple events match '{title}' — be more specific.",
            "candidates": candidates,
        }

    event   = matches[0]
    patched = calendar.events().patch(
        calendarId="primary",
        eventId=event["id"],
        body={"colorId": color_id},
    ).execute()

    raw_start = patched["start"].get("dateTime", patched["start"].get("date"))
    return {
        "updated":    True,
        "title":      patched.get("summary"),
        "start":      raw_start,
        "color_name": color_name,
    }


def delete_calendar_event(title: str, date_str: str,
                          end_date: str | None = None,
                          confirm: bool = False) -> dict:
    """
    Delete calendar events matching `title` within a date/range.

    SAFETY: confirm=False (default) previews without deleting.
    Set confirm=True only after the user explicitly confirms.
    """
    calendar, _ = _get_services()
    range_start, range_end = _resolve_dates(date_str, end_date)

    time_min = datetime(range_start.year, range_start.month, range_start.day,
                        0, 0, 0, tzinfo=_local_tz()).isoformat()
    time_max = datetime(range_end.year, range_end.month, range_end.day,
                        23, 59, 59, tzinfo=_local_tz()).isoformat()

    result = calendar.events().list(
        calendarId="primary", timeMin=time_min, timeMax=time_max,
        singleEvents=True, orderBy="startTime",
    ).execute()

    needle  = title.lower().strip()
    matches = []
    for e in result.get("items", []):
        raw_start = e["start"].get("dateTime", e["start"].get("date"))
        try:
            ev_date = date.fromisoformat(raw_start[:10])
        except ValueError:
            continue
        if not (range_start <= ev_date <= range_end):
            continue
        if needle in e.get("summary", "").lower():
            matches.append({"id": e["id"], "title": e.get("summary", "(no title)"),
                            "when": raw_start})

    if not matches:
        return {"matches": [], "count": 0,
                "message": f"No events matching '{title}' between {range_start} and {range_end}."}

    if not confirm:
        return {
            "needs_confirmation": True,
            "count":   len(matches),
            "matches": [{"title": m["title"], "when": m["when"]} for m in matches],
        }

    deleted = []
    for m in matches:
        calendar.events().delete(calendarId="primary", eventId=m["id"]).execute()
        deleted.append({"title": m["title"], "when": m["when"]})
    return {"deleted": deleted, "count": len(deleted)}


# ══════════════════════════════════════════════════════════════════════════════
# TASKS FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def list_tasks(list_name: str | None = None,
               due_date: str | None = None) -> list[dict]:
    """
    Return all open (incomplete) tasks.

    Args:
        list_name: If given, only tasks from that list. Omit for all lists.
                   "My Tasks" / "Мои задачи" → primary list.
        due_date:  ISO date "YYYY-MM-DD". If given, return only tasks whose
                   due date matches exactly. Filtered in Python after fetch.

    Returns:
        List of {title, due, list} dicts.
    """
    _, tasks_svc = _get_services()

    if list_name is not None:
        list_id, list_title = _resolve_list_name(list_name)
        lists_to_query = [(list_id, list_title)]
    else:
        lists_to_query = [(tl["id"], tl["title"]) for tl in get_task_lists()]

    out = []
    for lid, ltitle in lists_to_query:
        result = tasks_svc.tasks().list(
            tasklist=lid, showCompleted=False, showHidden=False,
        ).execute()
        for t in result.get("items", []):
            due_raw = t.get("due")
            out.append({
                "id":      t.get("id", ""),
                "title":   t.get("title", "(untitled)"),
                "due":     due_raw[:10] if due_raw else None,
                "list":    ltitle,
                "list_id": lid,
            })

    if due_date:
        # Normalise: accept "YYYY-MM-DD", strip anything longer
        target = due_date[:10]
        out = [t for t in out if t.get("due") == target]

    return out


def create_task(title: str,
                due_date: str | None = None,
                due_datetime: str | None = None,
                list_name: str | None = None) -> dict:
    """
    Create a new task in Google Tasks.

    ⚠ VERIFIED API LIMITATION: Tasks API v1 stores date-only in the `due`
    field. Any time you send is silently discarded. When due_datetime is
    provided, we set the task due date to that day AND also create a Calendar
    event at the exact time so it appears in Google Calendar with the time.

    Args:
        title:        Task title.
        due_date:     Date-only "YYYY-MM-DD" — sets task due with no time.
        due_datetime: Datetime "YYYY-MM-DDTHH:MM" or "YYYY-MM-DDTHH:MM:SS" —
                      sets task due to that date; creates a 30-min Calendar event
                      at that time (Tasks API cannot store the time itself).
        list_name:    Target list. Defaults to primary list if omitted.
                      Use actual list name: 'Work', 'Uni', 'Gym', 'Driving',
                      or omit/'My Tasks' for the primary list.

    Returns:
        {id, title, due_date, list} plus calendar_event if due_datetime was used.
    """
    _, tasks_svc = _get_services()

    if list_name is not None:
        list_id, list_title = _resolve_list_name(list_name)
    else:
        list_id, list_title = _default_list()

    body: dict = {"title": title}
    cal_event   = None

    if due_datetime:
        # Extract the date portion for the task's `due` field (API discards time).
        dt_part = due_datetime[:10]  # "YYYY-MM-DD"
        body["due"] = f"{dt_part}T00:00:00.000Z"

        # Create a Calendar event at the specified time for visibility.
        cal_start = _add_tz(due_datetime if "T" in due_datetime else f"{due_datetime}T00:00:00")
        # Parse the datetime to compute a 30-min end time.
        try:
            naive_dt = datetime.fromisoformat(
                due_datetime if "T" in due_datetime else f"{due_datetime}T00:00:00"
            )
            cal_end_naive = naive_dt + timedelta(minutes=30)
            cal_end = _add_tz(cal_end_naive.strftime("%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            cal_end = cal_start  # fallback: same time

        cal_event = create_calendar_event(title=title, start=cal_start, end=cal_end)
        cal_event["note"] = (
            "Calendar event created because Tasks API v1 cannot store a time. "
            "The task due date is set to the correct day."
        )

    elif due_date:
        body["due"] = f"{due_date}T00:00:00.000Z"

    created = tasks_svc.tasks().insert(tasklist=list_id, body=body).execute()

    result = {
        "id":       created["id"],
        "title":    created.get("title"),
        "due_date": created.get("due", "")[:10] if created.get("due") else None,
        "list":     list_title,
    }
    if cal_event:
        result["calendar_event"] = cal_event
    return result


def update_task(task_title: str,
                list_name: str | None = None,
                due_date: str | None = None,
                due_datetime: str | None = None,
                new_title: str | None = None) -> dict:
    """
    Update an existing task's due date, time, or title.

    Args:
        task_title:   Full or partial title of the task to update (case-insensitive).
        list_name:    List to search. Omit to search all lists.
        due_date:     New due date "YYYY-MM-DD" (date-only).
        due_datetime: New due datetime "YYYY-MM-DDTHH:MM" — sets task due date
                      to that day and creates a Calendar event at that time
                      (Tasks API cannot store time; same limitation as create_task).
        new_title:    Rename the task.

    Returns:
        {updated, title, due_date, list} or {error}.
    """
    _, tasks_svc = _get_services()

    if list_name is not None:
        list_id, list_title = _resolve_list_name(list_name)
        search_lists = [(list_id, list_title)]
    else:
        search_lists = [(tl["id"], tl["title"]) for tl in get_task_lists()]

    needle = task_title.lower()

    for lid, ltitle in search_lists:
        result = tasks_svc.tasks().list(
            tasklist=lid, showCompleted=False, showHidden=False,
        ).execute()
        match = next(
            (t for t in result.get("items", [])
             if needle in t.get("title", "").lower()),
            None,
        )
        if not match:
            continue

        patch:    dict = {}
        cal_event      = None

        if new_title:
            patch["title"] = new_title

        if due_datetime:
            dt_part = due_datetime[:10]
            patch["due"] = f"{dt_part}T00:00:00.000Z"

            cal_start = _add_tz(
                due_datetime if "T" in due_datetime else f"{due_datetime}T00:00:00"
            )
            try:
                naive_dt  = datetime.fromisoformat(
                    due_datetime if "T" in due_datetime else f"{due_datetime}T00:00:00"
                )
                cal_end   = _add_tz((naive_dt + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                cal_end = cal_start

            task_display_title = new_title or match.get("title", task_title)
            cal_event = create_calendar_event(
                title=task_display_title, start=cal_start, end=cal_end
            )
            cal_event["note"] = (
                "Calendar event created because Tasks API v1 cannot store a time."
            )

        elif due_date:
            patch["due"] = f"{due_date}T00:00:00.000Z"

        if not patch:
            return {"error": "Nothing to update — provide due_date, due_datetime, or new_title."}

        updated = tasks_svc.tasks().patch(
            tasklist=lid, task=match["id"], body=patch,
        ).execute()

        out = {
            "updated":  True,
            "id":       match["id"],
            "title":    updated.get("title"),
            "due_date": updated.get("due", "")[:10] if updated.get("due") else None,
            "list":     ltitle,
        }
        if cal_event:
            out["calendar_event"] = cal_event
        return out

    return {"error": f"No open task found matching '{task_title}'."}


def complete_task(task_title: str, list_name: str | None = None) -> dict:
    """
    Mark the first matching open task as completed.

    Args:
        task_title: Full or partial task title (case-insensitive substring).
        list_name:  If given, search only that list; otherwise search all lists.

    Returns:
        {completed, id, title, list} on success, or {error}.
    """
    _, tasks_svc = _get_services()

    if list_name is not None:
        list_id, list_title = _resolve_list_name(list_name)
        search_lists = [(list_id, list_title)]
    else:
        search_lists = [(tl["id"], tl["title"]) for tl in get_task_lists()]

    needle = task_title.lower()

    for lid, ltitle in search_lists:
        result = tasks_svc.tasks().list(
            tasklist=lid, showCompleted=False, showHidden=False,
        ).execute()
        match = next(
            (t for t in result.get("items", [])
             if needle in t.get("title", "").lower()),
            None,
        )
        if match:
            tasks_svc.tasks().patch(
                tasklist=lid, task=match["id"],
                body={"status": "completed"},
            ).execute()
            return {"completed": True, "id": match["id"],
                    "title": match["title"], "list": ltitle}

    return {"error": f"No open task found matching '{task_title}'."}


def complete_task_by_id(task_id: str, list_id: str, list_title: str) -> dict:
    """Mark a task done by its exact API id (used by inline-button callbacks)."""
    _, tasks_svc = _get_services()
    try:
        tasks_svc.tasks().patch(
            tasklist=list_id, task=task_id,
            body={"status": "completed"},
        ).execute()
        return {"completed": True, "id": task_id, "list": list_title}
    except Exception as exc:
        return {"error": str(exc)}


def delete_task_by_id(task_id: str, list_id: str, list_title: str) -> dict:
    """Delete a task by its exact API id (used by inline-button callbacks)."""
    _, tasks_svc = _get_services()
    try:
        tasks_svc.tasks().delete(tasklist=list_id, task=task_id).execute()
        return {"deleted": True, "id": task_id, "list": list_title}
    except Exception as exc:
        return {"error": str(exc)}


def list_completed_today() -> list[dict]:
    """
    Return tasks completed today across all task lists.
    Each dict: {title, list, completed_at}
    """
    _, tasks_svc = _get_services()
    today = date.today().isoformat()
    out = []
    for tl in get_task_lists():
        lid, ltitle = tl["id"], tl["title"]
        result = tasks_svc.tasks().list(
            tasklist=lid,
            showCompleted=True,
            showHidden=True,
        ).execute()
        for t in result.get("items", []):
            if t.get("status") != "completed":
                continue
            completed_raw = t.get("completed", "")
            if not completed_raw.startswith(today):
                continue
            out.append({
                "title":        t.get("title", "(untitled)"),
                "list":         ltitle,
                "completed_at": completed_raw[:16].replace("T", " "),
            })
    return out
