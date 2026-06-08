# Telegram LLM Assistant

A personal Telegram bot that combines a local LLM (via Ollama) with Google Calendar, Tasks, and Gmail — plus weather, reminders, voice input, calorie/habit logging, and more. All inference runs locally; no data leaves your machine except to Google's APIs.

---

## Features

| Category | What it does |
|---|---|
| **Chat** | Conversational assistant backed by a local Ollama model |
| **Google Calendar** | Create, list, and color-code events; `/event` guided wizard |
| **Google Tasks** | Add, complete, list tasks across all your task lists |
| **Gmail** | Read unread email summaries (`/inbox`) — read-only scope |
| **Weather** | Current + forecast via Open-Meteo (free, no API key) |
| **Reminders** | Persistent reminders stored in SQLite, checked every 30 s |
| **Voice input** | Send a voice note — transcribed locally with Whisper (faster-whisper) |
| **Task templates** | Save and rerun sets of tasks with `/template` |
| **Daily digest** | Evening summary of completed tasks + upcoming events (`/digest`) |
| **Morning briefing** | Weather + today's agenda sent automatically each morning |
| **Calorie & habit log** | Neutral log-and-recall; no advice, no targets (`/log`) |
| **Memory** | Persistent key/value facts the bot remembers across sessions |
| **Web search** | DuckDuckGo search via agent tool call |

---

## Architecture

The bot follows a **model-fuzzy / code-deterministic** principle:

- The LLM decides *what* to do (intent, tool selection, natural language) but **never formats structured data** — all calendar listings, task lists, reminders, and log entries are formatted by Python code.
- Tool calls go through a strict JSON schema; hallucinated fields are ignored.
- Google API calls, Whisper transcription, and Gmail fetches all run in `asyncio.to_thread()` — independent of the single-worker LLM executor — so they never block each other.

```
telegram_bot.py       ← PTB v20 async handlers, ConversationHandlers, scheduler
    │
    ├── agent.py      ← Ollama tool-call loop, system prompt, TOOL_FUNCTIONS registry
    │       ├── tools.py        ← Google Calendar + Tasks wrappers
    │       ├── gmail_tools.py  ← Gmail read-only helpers
    │       ├── weather.py      ← Open-Meteo geocode + forecast
    │       ├── memory.py       ← SQLite key/value store
    │       ├── reminders.py    ← SQLite reminder store
    │       ├── log_store.py    ← SQLite calorie + habit log
    │       └── templates.py    ← SQLite task template store
    │
    ├── auth.py       ← Google OAuth2 helpers
    └── whisper_stt.py← faster-whisper transcription (CPU int8)
```

---

## Tech stack

- **Python 3.11+** with asyncio
- **[python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)** v20+
- **[Ollama](https://ollama.com/)** — local LLM server (default model: `qwen2.5:7b`, configurable)
- **[faster-whisper](https://github.com/SYSTRAN/faster-whisper)** — local Whisper STT (CPU int8, no GPU required)
- **Google APIs** — Calendar v3, Tasks v1, Gmail v1 via `google-api-python-client`
- **[DuckDuckGo Search](https://pypi.org/project/duckduckgo-search/)** — web search
- **SQLite** (stdlib) — all persistent state: memory, templates, reminders, logs
- **Open-Meteo** — weather (free, no key)

---

## Setup

### 1. Prerequisites

- Python 3.11+
- [Ollama](https://ollama.com/) installed and running (`ollama serve`)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A Google Cloud project with the Calendar, Tasks, and Gmail APIs enabled

### 2. Google OAuth credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → APIs & Services → Credentials
2. Create an OAuth 2.0 Client ID (Desktop app)
3. Download as `credentials.json` and place it in this directory
4. On first run, a browser window will open for you to authorize access
5. The token is cached in `token.json` (gitignored)

Required scopes:
```
https://www.googleapis.com/auth/calendar
https://www.googleapis.com/auth/tasks
https://www.googleapis.com/auth/gmail.readonly
```

### 3. Install dependencies

```bash
python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

pip install -r requirements.txt
```

### 4. Configure environment

```bash
cp .env.example .env
# Edit .env and fill in your values
```

Key variables:

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your bot token from BotFather |
| `TELEGRAM_USER_ID` | Your numeric Telegram ID (message @userinfobot) |
| `DEFAULT_CITY` | Default city for weather lookups |
| `TIMEZONE` | Your local timezone (e.g. `America/New_York`) |

### 5. Run

```bash
python telegram_bot.py
```

The bot will send a startup banner to your Telegram when it's ready.

---

## Commands

```
/help [topic]       Show help. Topics: chat, calendar, tasks, gmail, weather,
                    reminders, voice, templates, digest, log, memory, system

/briefing           Morning briefing: weather + today's events
/digest             Evening digest: completed tasks + upcoming events

/event              Guided wizard to create a calendar event (with color picker)
/template           Manage task templates: list | add | run | delete

/inbox [n]          Summarize n unread emails (default 10)
/reminders          List your pending reminders
/log [date]         Show calorie + habit log for a date

/weather [city]     Current weather; /weather setdefault <city> to change default
/memory view        Show stored facts
/clear              Clear conversation history

/cancel             Cancel any in-progress wizard
```

---

## Windows autostart (optional)

`setup-autostart.ps1` creates a Windows Scheduled Task that starts the bot on login and on wake from sleep. Run it once as Administrator:

```powershell
# In an elevated PowerShell:
.\setup-autostart.ps1
```

---

## Why this way?

**Local-first**: The LLM never sees your Google data directly — it calls tools, and tools return structured data that Python formats. Your calendar, emails, and task lists stay on your machine or travel only to Google's servers.

**Deterministic output for structured data**: Listing your tasks or calendar events in a consistent format is a code problem, not an LLM problem. The model picks the tool and arguments; Python formats the result. This eliminates hallucinated event titles, wrong dates, and invented tasks.

**Single SQLite file**: All local state (memory, templates, reminders, logs) lives in one `memory.db` file — easy to back up, inspect, or wipe.

---

## Contributing

Pull requests welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

---

## License

MIT — see [LICENSE](LICENSE).
