# Contributing

Thanks for your interest in contributing!

## Getting started

1. Fork the repository and clone your fork
2. Follow the setup steps in [README.md](README.md)
3. Create a branch for your change: `git checkout -b my-feature`

## Code style

- Python 3.11+, formatted with [black](https://github.com/psf/black) (line length 100)
- Type hints on all public functions
- No comments explaining *what* — only *why* when non-obvious
- No LLM calls to format structured data — keep output formatting in Python code

## Project structure

```
agent.py          Tool-call loop and system prompt
auth.py           Google OAuth2
gmail_tools.py    Gmail read helpers
log_store.py      Calorie + habit SQLite store
memory.py         Key/value SQLite store
reminders.py      Reminder SQLite store
templates.py      Task template SQLite store
telegram_bot.py   PTB handlers and scheduler
tools.py          Google Calendar + Tasks wrappers
weather.py        Open-Meteo weather
whisper_stt.py    Local Whisper transcription
assistant_toggle.py  Start/stop helpers (Windows)
setup-autostart.ps1  Windows Scheduled Task setup
```

## Pull request checklist

- [ ] No secrets, tokens, or personal data in any file
- [ ] New tools registered in both `TOOL_SCHEMAS` and `TOOL_FUNCTIONS` in `agent.py`
- [ ] New commands registered in `application.add_handler()` and documented in `_HELP_TOPICS`
- [ ] SQLite migrations are backward-compatible (`IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`)
- [ ] `requirements.txt` updated if new dependencies added

## Reporting issues

Please open a GitHub issue with steps to reproduce, your Python version, and the full traceback.
