<div align="center">

# 🤖 self-hosted-ai-assistant

**A personal Telegram bot powered by a local LLM — with Google Calendar, Tasks, Gmail, voice input, reminders, and more. Nothing leaves your machine except Google API calls.**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Ollama](https://img.shields.io/badge/Ollama-Local_LLM-000000?style=for-the-badge&logo=ollama&logoColor=white)](https://ollama.com/)
[![Telegram](https://img.shields.io/badge/Telegram-Bot-26A5E4?style=for-the-badge&logo=telegram&logoColor=white)](https://core.telegram.org/bots)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge)](LICENSE)

</div>

---

## What is this?

Most AI assistants send your data to a cloud. This one doesn't — the LLM runs locally via [Ollama](https://ollama.com/), voice transcription runs locally via [Whisper](https://github.com/SYSTRAN/faster-whisper), and all persistent state lives in a single SQLite file on your machine. The only external calls are to Google's APIs (Calendar, Tasks, Gmail) and Open-Meteo for weather.

The design follows a **model-fuzzy / code-deterministic** principle: the LLM decides *what* to do — intent, tool selection, natural language — but **never formats structured data**. Calendar listings, task lists, reminders, and log entries are all formatted by Python code. Tool calls go through a strict JSON schema; hallucinated fields are ignored.

---

## ✨ Features

| | |
|---|---|
| 💬 **Chat** | Conversational assistant backed by any Ollama-compatible model |
| 📅 **Google Calendar** | Create, list, and color-code events; `/event` guided wizard |
| ✅ **Google Tasks** | Add, complete, and list tasks across all your task lists |
| 📧 **Gmail** | Summarize unread emails (`/inbox`) — read-only, never modifies |
| 🌤 **Weather** | Current + forecast via Open-Meteo — free, no API key |
| ⏰ **Reminders** | Persistent SQLite-backed reminders, checked every 30 s |
| 🎤 **Voice input** | Send a voice note — transcribed locally with Whisper (CPU, no GPU needed) |
| 📋 **Task templates** | Save and rerun named sets of tasks with `/template` |
| 🌅 **Morning briefing** | Weather + today's agenda sent automatically each morning |
| 🌙 **Daily digest** | Evening summary of completed tasks + upcoming events (`/digest`) |
| 🥗 **Calorie & habit log** | Neutral log-and-recall — no advice, no targets (`/log`) |
| 🧠 **Memory** | Persistent key/value facts the bot remembers across sessions |
| 🔍 **Web search** | DuckDuckGo search via agent tool call |

---

## 🏗️ How it works

```
Telegram app (your phone)
        │
        │  (HTTPS — python-telegram-bot)
        ▼
telegram_bot.py — PTB v20 async handlers, ConversationHandlers, scheduler
        │
        ├── agent.py — Ollama tool-call loop
        │       │
        │       ├── tools.py          Google Calendar + Tasks wrappers
        │       ├── gmail_tools.py    Gmail read-only helpers
        │       ├── weather.py        Open-Meteo geocode + forecast
        │       ├── memory.py         SQLite key/value store
        │       ├── reminders.py      SQLite reminder store
        │       ├── log_store.py      SQLite calorie + habit log
        │       └── templates.py      SQLite task template store
        │
        ├── auth.py         Google OAuth2 helpers
        └── whisper_stt.py  faster-whisper transcription (CPU int8)
                │
                ▼
         asyncio.to_thread()  ← Gmail, Whisper, and Google API calls each run
                               in their own thread — independent of the LLM executor
```

Each user message goes through a tool-call loop: the model picks a tool (or none), Python executes it, the result is fed back, and the loop repeats until the model produces a final text response.

---

## 🚀 Quick Start

### 1. Prerequisites

- Python 3.11+
- [Ollama](https://ollama.com/) installed and running: `ollama serve`
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A Google Cloud project with Calendar, Tasks, and Gmail APIs enabled

### 2. Clone and install

```bash
git clone https://github.com/mishgoldenberg/self-hosted-ai-assistant.git
cd self-hosted-ai-assistant

python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

pip install -r requirements.txt
```

### 3. Google OAuth credentials

1. Open [Google Cloud Console](https://console.cloud.google.com/) → APIs & Services → Credentials
2. Create an **OAuth 2.0 Client ID** (Desktop app type)
3. Download it as `credentials.json` and place it in the project directory
4. On first run a browser window opens — authorise access, token is cached in `token.json`

Required scopes:
```
https://www.googleapis.com/auth/calendar
https://www.googleapis.com/auth/tasks
https://www.googleapis.com/auth/gmail.readonly
```

### 4. Configure

```bash
cp .env.example .env
# Edit .env and fill in your values
```

### 5. Pull a model and run

```bash
ollama pull qwen2.5:7b   # or any model you prefer
python telegram_bot.py
```

The bot sends a startup banner to your Telegram chat when it's ready.

---

## ⚙️ Configuration Reference

| Variable | Required | Description |
|---|:---:|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | Bot token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_USER_ID` | ✅ | Your numeric Telegram ID — message [@userinfobot](https://t.me/userinfobot) |
| `DEFAULT_CITY` | | Default city for weather lookups (changeable at runtime) |
| `TIMEZONE` | | Your local timezone, e.g. `America/New_York`, `Europe/London` |

---

## 💬 Commands

| Command | Description |
|---|---|
| `/help [topic]` | Help index, or deep-dive on a topic: `chat` `calendar` `tasks` `gmail` `weather` `reminders` `voice` `templates` `digest` `log` `memory` `system` |
| `/briefing` | Morning briefing — weather + today's calendar events |
| `/digest` | Evening digest — completed tasks + upcoming events |
| `/event` | Guided wizard to create a calendar event (title, time, reminder, color) |
| `/template` | Task templates — `list` · `add` · `run` · `delete` |
| `/inbox [n]` | Summarize *n* unread emails (default 10) — read-only |
| `/reminders` | List your pending reminders |
| `/log [date]` | Show calorie + habit log for a date (default: today) |
| `/weather [city]` | Current weather; `/weather setdefault <city>` to change default |
| `/memory view` | Show all stored facts |
| `/clear` | Clear conversation history |
| `/cancel` | Cancel any in-progress wizard |

---

## 🗂️ Project Structure

```
self-hosted-ai-assistant/
├── agent.py            Ollama tool-call loop, system prompt, tool registry
├── auth.py             Google OAuth2 credential handling
├── chat.py             Low-level chat completion wrapper
├── gmail_tools.py      Gmail read helpers
├── log_store.py        SQLite calorie + habit log
├── memory.py           SQLite key/value memory store
├── reminders.py        SQLite reminder store
├── search.py           DuckDuckGo search wrapper
├── telegram_bot.py     PTB v20 handlers, wizards, scheduler
├── templates.py        SQLite task template store
├── tools.py            Google Calendar + Tasks API wrappers
├── weather.py          Open-Meteo geocode + forecast
├── whisper_stt.py      faster-whisper local transcription
├── assistant_toggle.py Start / stop helpers (Windows)
├── setup-autostart.ps1 Windows Scheduled Task setup (optional)
├── requirements.txt
├── .env.example
└── LICENSE
```

---

## 🔧 Windows Autostart (optional)

`setup-autostart.ps1` creates a Scheduled Task that starts the bot on login and on wake from sleep. Run once as Administrator:

```powershell
.\setup-autostart.ps1
```

Configures: Fast Startup off, Hybrid Sleep off, NIC Power Saving off, task triggers on logon (10 s delay) and on wake from sleep (20 s delay).

---

## 📄 License

[MIT](LICENSE) © 2026 Michael Goldenberg
