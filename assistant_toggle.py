"""
assistant_toggle.py — Desktop on/off toggle for the Personal Assistant stack.

Manages two components:
  • Ollama        (native Windows process)
  • Telegram Bot  (Python subprocess)

Also manages a power profile:
  ON  → save current settings, apply assistant profile (balanced, no sleep, display 5 min)
  OFF → restore saved settings (or Windows defaults if none were saved)

Requires Administrator for power changes. If not elevated, prompts to relaunch.

Launch by double-clicking assistant_toggle.bat, or run:
    venv/Scripts/python assistant_toggle.py
"""

import ctypes
import datetime
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import winreg
from pathlib import Path
import tkinter as tk
import tkinter.messagebox as mb

# Import memory store — same sqlite db the bot uses
sys.path.insert(0, str(Path(__file__).parent))
import memory as _memory

# ── PATH bootstrap ─────────────────────────────────────────────────────────────
def _full_path() -> str:
    parts = [os.environ.get("PATH", "")]
    for hive, sub in [
        (winreg.HKEY_LOCAL_MACHINE,
         r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, r"Environment"),
    ]:
        try:
            with winreg.OpenKey(hive, sub) as k:
                val, _ = winreg.QueryValueEx(k, "PATH")
                parts.append(os.path.expandvars(val))
        except OSError:
            pass
    return ";".join(parts)

os.environ["PATH"] = _full_path()

# ── Config paths ───────────────────────────────────────────────────────────────
# Auto-detect: the toggle app lives in the same directory as the bot.
ASSISTANT_DIR = Path(__file__).resolve().parent
VENV_PYTHON   = ASSISTANT_DIR / "venv" / "Scripts" / "python.exe"
BOT_SCRIPT    = ASSISTANT_DIR / "telegram_bot.py"
PID_FILE      = ASSISTANT_DIR / ".bot_pid"
POWER_STATE_FILE = ASSISTANT_DIR / ".power_state.json"

_lad = os.environ.get("LOCALAPPDATA", "")
OLLAMA_EXE: Path | None = None
for _cand in [
    Path(_lad) / "Programs" / "Ollama" / "ollama.exe",
    Path(shutil.which("ollama") or ""),
]:
    if _cand.exists():
        OLLAMA_EXE = _cand
        break

# ── Power plan constants ───────────────────────────────────────────────────────
BALANCED_PLAN = "381b4222-f694-41f0-9685-ff5bb260df2e"
_SG_SLEEP     = "238c9fa8-0aad-41ed-83f4-97be242c8f20"
_SET_STANDBY  = "29f6c1db-86da-48c5-9fdb-f2b67b1f44da"
_SET_HIBER    = "9d7815a6-7ee4-497e-8888-515a05f02364"
_SG_DISPLAY   = "7516b95f-f776-4464-8c53-06167f40cc99"
_SET_MONITOR  = "3c0bc021-c8a8-4e07-a973-6b14cbcb2b7e"

# ── Colours (Catppuccin Mocha palette) ────────────────────────────────────────
C = {
    "bg":      "#1e1e2e",
    "surface": "#313244",
    "text":    "#cdd6f4",
    "sub":     "#6c7086",
    "green":   "#a6e3a1",
    "red":     "#f38ba8",
    "yellow":  "#f9e2af",
    "blue":    "#89b4fa",
    "dark":    "#11111b",
    "orange":  "#fab387",
}

# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _sh(cmd: str, timeout: int = 10) -> tuple[str, int]:
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=timeout,
                           creationflags=_NO_WINDOW)
        return r.stdout.strip(), r.returncode
    except Exception:
        return "", 1


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════════
# STATUS CHECKS  — each returns a plain bool; safe to call from any thread
# ══════════════════════════════════════════════════════════════════════════════

def ok_ollama() -> bool:
    try:
        with socket.create_connection(("localhost", 11434), timeout=1):
            return True
    except OSError:
        return False


def ok_bot() -> bool:
    if not PID_FILE.exists():
        return False
    pid_txt = PID_FILE.read_text(encoding="utf-8").strip()
    if not pid_txt.isdigit():
        return False
    out, _ = _sh(f'tasklist /FI "PID eq {pid_txt}" /NH')
    return pid_txt in out


def ollama_procs() -> list[str]:
    found = []
    for img in ("ollama.exe", "ollama app.exe"):
        out, _ = _sh(f'tasklist /FI "IMAGENAME eq {img}" /NH')
        if img.lower() in out.lower():
            found.append(img)
    return found


def ollama_down() -> bool:
    return (not ok_ollama()) and (not ollama_procs())


# ══════════════════════════════════════════════════════════════════════════════
# POWER MANAGEMENT  — all require admin; call is_admin() before invoking
# ══════════════════════════════════════════════════════════════════════════════

def _get_active_plan() -> str:
    out, _ = _sh("powercfg /getactivescheme")
    m = re.search(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        out, re.I,
    )
    return m.group(1).lower() if m else ""


def _get_timeout(subgroup: str, setting: str) -> int:
    """Return the current AC timeout in seconds (-1 on failure)."""
    out, _ = _sh(f"powercfg /query SCHEME_CURRENT {subgroup} {setting}")
    m = re.search(r"Current AC Power Setting Index:\s*(0x[0-9a-fA-F]+)", out)
    if not m:
        return -1
    return int(m.group(1), 16)


def _get_user_env(name: str) -> str:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            val, _ = winreg.QueryValueEx(k, name)
            return val
    except OSError:
        return ""


def save_power_state() -> dict:
    """Capture active plan + AC timeouts + OLLAMA_KEEP_ALIVE to disk."""
    state = {
        "plan_guid":      _get_active_plan(),
        "standby_ac":     _get_timeout(_SG_SLEEP, _SET_STANDBY),
        "hibernate_ac":   _get_timeout(_SG_SLEEP, _SET_HIBER),
        "monitor_ac":     _get_timeout(_SG_DISPLAY, _SET_MONITOR),
        "keep_alive_prev": _get_user_env("OLLAMA_KEEP_ALIVE"),
    }
    POWER_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


def apply_assistant_power(log):
    """Switch to Balanced plan, no sleep/hibernate, display off 5 min, KEEP_ALIVE=5m."""
    _sh(f"powercfg /setactive {BALANCED_PLAN}")
    _sh("powercfg /change standby-timeout-ac 0")
    _sh("powercfg /change hibernate-timeout-ac 0")
    _sh("powercfg /change monitor-timeout-ac 5")
    # Persist env var for future sessions and set it now so Ollama inherits it.
    _sh('powershell -NoProfile -Command '
        '"[System.Environment]::SetEnvironmentVariable(\'OLLAMA_KEEP_ALIVE\', \'5m\', \'User\')"')
    os.environ["OLLAMA_KEEP_ALIVE"] = "5m"
    log("Power: Balanced, no sleep/hibernate, display 5 min, KEEP_ALIVE=5m ✓")


_POWER_DEFAULTS = {
    "plan_guid":    BALANCED_PLAN,
    "standby_ac":   1800,   # 30 min
    "hibernate_ac": 0,      # off
    "monitor_ac":   600,    # 10 min
    "keep_alive_prev": "",
}


def restore_power_state(log):
    """Restore previously saved power settings, or apply sensible defaults."""
    used_defaults = False
    state = _POWER_DEFAULTS.copy()

    if POWER_STATE_FILE.exists():
        try:
            loaded = json.loads(POWER_STATE_FILE.read_text(encoding="utf-8"))
            state.update(loaded)
        except Exception:
            used_defaults = True
    else:
        used_defaults = True

    plan        = state.get("plan_guid") or BALANCED_PLAN
    standby_s   = state.get("standby_ac",   _POWER_DEFAULTS["standby_ac"])
    hibernate_s = state.get("hibernate_ac", _POWER_DEFAULTS["hibernate_ac"])
    monitor_s   = state.get("monitor_ac",   _POWER_DEFAULTS["monitor_ac"])
    prev_ka     = state.get("keep_alive_prev", "")

    # powercfg /change expects minutes; 0 = never; negative means query failed → use default
    def _to_min(seconds: int, default_min: int) -> int:
        if seconds < 0:
            return default_min
        return seconds // 60

    standby_min   = _to_min(standby_s,   30)
    hibernate_min = _to_min(hibernate_s,  0)
    monitor_min   = _to_min(monitor_s,   10)

    _sh(f"powercfg /setactive {plan}")
    _sh(f"powercfg /change standby-timeout-ac {standby_min}")
    _sh(f"powercfg /change hibernate-timeout-ac {hibernate_min}")
    _sh(f"powercfg /change monitor-timeout-ac {monitor_min}")

    # Clear OLLAMA_KEEP_ALIVE (restore to previous value, which may be empty).
    _sh('powershell -NoProfile -Command '
        f'"[System.Environment]::SetEnvironmentVariable(\'OLLAMA_KEEP_ALIVE\', \'{prev_ka}\', \'User\')"')
    os.environ.pop("OLLAMA_KEEP_ALIVE", None)

    POWER_STATE_FILE.unlink(missing_ok=True)

    if used_defaults:
        log(f"Power: no saved state — defaults applied "
            f"(sleep {standby_min}min, display {monitor_min}min, "
            f"hibernate {'off' if hibernate_min == 0 else f'{hibernate_min}min'})")
    else:
        log("Power: normal (restored to pre-assistant settings) ✓")


# ══════════════════════════════════════════════════════════════════════════════
# START / STOP ACTIONS
# ══════════════════════════════════════════════════════════════════════════════

def act_start_ollama(log) -> bool:
    if ok_ollama():
        log("Ollama: already running")
        return True
    if not OLLAMA_EXE:
        log("ERROR: ollama.exe not found — is Ollama installed?")
        return False
    log("Starting Ollama…")
    subprocess.Popen([str(OLLAMA_EXE), "serve"],
                     env=os.environ.copy(),
                     creationflags=subprocess.CREATE_NO_WINDOW)
    for _ in range(20):
        time.sleep(1)
        if ok_ollama():
            log("Ollama: ✓ running")
            return True
    log("ERROR: Ollama didn't respond within 20 s")
    return False


def act_start_bot(log) -> bool:
    if ok_bot():
        log("Telegram bot: already running")
        return True
    env_file = ASSISTANT_DIR / ".env"
    if not env_file.exists():
        log("ERROR: .env missing — Telegram bot not started")
        return False
    log("Starting Telegram bot…")
    try:
        proc = subprocess.Popen(
            [str(VENV_PYTHON), str(BOT_SCRIPT)],
            cwd=str(ASSISTANT_DIR),
            env=os.environ.copy(),
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        PID_FILE.write_text(str(proc.pid), encoding="utf-8")
        time.sleep(2)
        if ok_bot():
            log(f"Telegram bot: ✓ running (PID {proc.pid})")
        else:
            log(f"Telegram bot: started (PID {proc.pid})")
        return True
    except Exception as exc:
        log(f"ERROR starting bot: {exc}")
        return False


def act_stop_bot(log):
    if not PID_FILE.exists():
        log("Telegram bot: not running")
        return
    pid_txt = PID_FILE.read_text(encoding="utf-8").strip()
    if pid_txt.isdigit():
        _sh(f"taskkill /F /PID {pid_txt}")
        log(f"Telegram bot: stopped (PID {pid_txt})")
    PID_FILE.unlink(missing_ok=True)


def act_stop_ollama(log) -> bool:
    if ollama_down():
        log("Ollama: not running")
        return True

    log("Stopping Ollama…")
    _sh('powershell -NoProfile -Command '
        '"Get-Service Ollama,OllamaService -ErrorAction SilentlyContinue '
        '| Stop-Service -Force -ErrorAction SilentlyContinue"')
    _sh('taskkill /F /T /IM "ollama app.exe"')
    _sh('taskkill /F /T /IM "ollama.exe"')

    for _ in range(10):
        time.sleep(1)
        if ollama_down():
            log("Ollama: stopped ✓  (VRAM freed)")
            return True
        _sh('taskkill /F /T /IM "ollama app.exe"')
        _sh('taskkill /F /T /IM "ollama.exe"')

    remaining = ollama_procs()
    port = "responding" if ok_ollama() else "down"
    log(f"WARNING: Ollama still up — port {port}, procs {remaining or 'none'}")
    return False


# ══════════════════════════════════════════════════════════════════════════════
# GUI
# ══════════════════════════════════════════════════════════════════════════════

REFRESH_MS = 3_000
WINDOW_W   = 340


class AssistantApp(tk.Tk):

    def __init__(self, admin: bool = False):
        super().__init__()
        self._admin = admin
        self.title("Personal Assistant")
        self.configure(bg=C["bg"])
        self.resizable(False, False)

        self._st: dict[str, bool] = {"ollama": False, "bot": False}
        self._busy = False

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.after(200, self._poll)

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        pad = 14

        # Admin warning banner (shown only when not elevated)
        if not self._admin:
            warn = tk.Frame(self, bg="#45475a", pady=4)
            warn.pack(fill="x")
            tk.Label(warn, text="⚠  Not running as Administrator — power profile disabled",
                     bg="#45475a", fg=C["yellow"],
                     font=("Segoe UI", 8)).pack()

        tk.Label(self, text="Personal Assistant",
                 bg=C["bg"], fg=C["text"],
                 font=("Segoe UI Semibold", 13), pady=10).pack()

        _sep(self)

        # Component status rows
        box = tk.Frame(self, bg=C["bg"], padx=pad, pady=8)
        box.pack(fill="x")

        self._dots:  dict[str, tk.Label] = {}
        self._stats: dict[str, tk.Label] = {}

        for key, name in (("ollama", "Ollama"),
                          ("bot",    "Telegram Bot")):
            row = tk.Frame(box, bg=C["bg"])
            row.pack(fill="x", pady=3)

            dot = tk.Label(row, text="●", font=("Segoe UI", 12),
                           bg=C["bg"], fg=C["yellow"], width=2)
            dot.pack(side="left")

            tk.Label(row, text=name, font=("Segoe UI", 10),
                     bg=C["bg"], fg=C["text"],
                     width=14, anchor="w").pack(side="left", padx=(6, 0))

            stat = tk.Label(row, text="checking…", font=("Segoe UI", 9),
                            bg=C["bg"], fg=C["sub"], anchor="w")
            stat.pack(side="left")

            self._dots[key]  = dot
            self._stats[key] = stat

        # Power mode row
        prow = tk.Frame(box, bg=C["bg"])
        prow.pack(fill="x", pady=3)

        self._power_dot = tk.Label(prow, text="●", font=("Segoe UI", 12),
                                   bg=C["bg"], fg=C["yellow"], width=2)
        self._power_dot.pack(side="left")

        tk.Label(prow, text="Power", font=("Segoe UI", 10),
                 bg=C["bg"], fg=C["text"],
                 width=14, anchor="w").pack(side="left", padx=(6, 0))

        self._power_stat = tk.Label(prow, text="checking…", font=("Segoe UI", 9),
                                    bg=C["bg"], fg=C["sub"], anchor="w")
        self._power_stat.pack(side="left")

        _sep(self)

        # Toggle button
        bf = tk.Frame(self, bg=C["bg"], pady=12)
        bf.pack()

        self._btn = tk.Button(
            bf, text="▶  START ALL",
            font=("Segoe UI Semibold", 11), width=18,
            bg=C["green"], fg=C["dark"],
            activebackground=C["green"], activeforeground=C["dark"],
            relief="flat", bd=0, cursor="hand2",
            command=self._toggle,
        )
        self._btn.pack()

        # Memory shortcut button
        tk.Button(
            bf, text="🧠  Memory",
            font=("Segoe UI", 9), width=18,
            bg=C["surface"], fg=C["text"],
            activebackground=C["surface"], activeforeground=C["text"],
            relief="flat", bd=0, cursor="hand2",
            command=lambda: _MemoryWindow(self),
        ).pack(pady=(6, 0))

        _sep(self)

        # Footer log
        foot = tk.Frame(self, bg=C["bg"], padx=pad, pady=8)
        foot.pack(fill="x")

        self._url_var = tk.StringVar(value="")
        tk.Label(foot, textvariable=self._url_var,
                 bg=C["bg"], fg=C["blue"],
                 font=("Segoe UI", 8), anchor="w").pack(fill="x")

        self._log_var = tk.StringVar(value="Checking status…")
        tk.Label(foot, textvariable=self._log_var,
                 bg=C["bg"], fg=C["sub"],
                 font=("Segoe UI", 8),
                 wraplength=WINDOW_W - pad * 2,
                 justify="left", anchor="w").pack(fill="x")

    # ── Status poll ───────────────────────────────────────────────────────────

    def _poll(self):
        if not self._busy:
            threading.Thread(target=self._check_all, daemon=True).start()
        self.after(REFRESH_MS, self._poll)

    def _check_all(self):
        st = {"ollama": ok_ollama(), "bot": ok_bot()}
        self.after(0, self._apply, st)

    def _apply(self, st: dict):
        self._st = st

        for key, running in st.items():
            self._dots[key].config(fg=C["green"] if running else C["red"])
            self._stats[key].config(
                text="Running" if running else "Stopped",
                fg=C["green"] if running else C["red"],
            )

        # Power indicator — derive from state file presence
        power_on = POWER_STATE_FILE.exists()
        if not self._admin:
            self._power_dot.config(fg=C["sub"])
            self._power_stat.config(text="n/a (no admin)", fg=C["sub"])
        elif power_on:
            self._power_dot.config(fg=C["green"])
            self._power_stat.config(text="assistant mode", fg=C["green"])
        else:
            self._power_dot.config(fg=C["blue"])
            self._power_stat.config(text="normal", fg=C["blue"])

        all_on  = all(st.values())
        all_off = not any(st.values())

        if self._busy:
            self._btn.config(text="  working…", bg=C["yellow"],
                             fg=C["dark"], state="disabled")
        elif all_on:
            self._btn.config(text="■  STOP ALL", bg=C["red"],
                             fg=C["dark"], state="normal")
        else:
            label = "▶  START ALL" if all_off else "▶  START ALL  (partial)"
            self._btn.config(text=label, bg=C["green"],
                             fg=C["dark"], state="normal")

        self._url_var.set("")

    # ── Toggle ────────────────────────────────────────────────────────────────

    def _toggle(self):
        all_on = all(self._st.values())
        target = self._do_stop if all_on else self._do_start
        self._busy = True
        self._btn.config(state="disabled", text="  working…", bg=C["yellow"])
        threading.Thread(target=target, daemon=True).start()

    def _log(self, msg: str):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.after(0, self._log_var.set, f"{ts}  {msg}")

    def _done(self):
        self._busy = False
        threading.Thread(target=self._check_all, daemon=True).start()

    def _do_start(self):
        try:
            if self._admin:
                self._log("Saving power settings…")
                save_power_state()
                apply_assistant_power(self._log)
            else:
                self._log("⚠ Not admin — power profile skipped, stack starting…")
            act_start_ollama(self._log)
            act_start_bot(self._log)
            self._log("All systems started ✓")
        except Exception as exc:
            self._log(f"ERROR: {exc}")
        finally:
            self.after(1000, self._done)

    def _do_stop(self):
        try:
            act_stop_bot(self._log)
            act_stop_ollama(self._log)

            still_up = []
            if not ollama_down():
                still_up.append("Ollama")
            if ok_bot():
                still_up.append("Telegram Bot")

            if still_up:
                self._log("STOP INCOMPLETE — still running: " + ", ".join(still_up))
            else:
                self._log("Stack down ✓  VRAM freed")

            if self._admin:
                restore_power_state(self._log)
            else:
                self._log("⚠ Not admin — power settings NOT restored")
        except Exception as exc:
            self._log(f"ERROR: {exc}")
        finally:
            self.after(1000, self._done)


# ── Memory window ─────────────────────────────────────────────────────────────

class _MemoryWindow(tk.Toplevel):
    """Popup that shows, deletes, and clears memories from the shared SQLite store."""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Memory")
        self.configure(bg=C["bg"])
        self.resizable(False, False)
        self.grab_set()

        pad = 12
        tk.Label(self, text="Stored Memories",
                 bg=C["bg"], fg=C["text"],
                 font=("Segoe UI Semibold", 12), pady=8).pack()

        tk.Frame(self, bg=C["surface"], height=1).pack(fill="x")

        # Listbox + scrollbar
        list_frame = tk.Frame(self, bg=C["bg"], padx=pad, pady=8)
        list_frame.pack(fill="both")

        sb = tk.Scrollbar(list_frame, orient="vertical")
        self._lb = tk.Listbox(
            list_frame,
            bg=C["surface"], fg=C["text"],
            selectbackground=C["blue"], selectforeground=C["dark"],
            font=("Segoe UI", 9),
            width=54, height=12,
            yscrollcommand=sb.set,
            borderwidth=0, highlightthickness=0,
        )
        sb.config(command=self._lb.yview)
        self._lb.pack(side="left", fill="both")
        sb.pack(side="left", fill="y")

        tk.Frame(self, bg=C["surface"], height=1).pack(fill="x")

        btn_row = tk.Frame(self, bg=C["bg"], padx=pad, pady=10)
        btn_row.pack()

        def _btn(parent, label, color, cmd):
            return tk.Button(parent, text=label,
                             font=("Segoe UI", 9), width=14,
                             bg=color, fg=C["dark"],
                             activebackground=color, activeforeground=C["dark"],
                             relief="flat", bd=0, cursor="hand2",
                             command=cmd)

        _btn(btn_row, "Delete selected", C["red"],   self._delete_selected).pack(side="left", padx=4)
        _btn(btn_row, "Clear all",       C["orange"], self._clear_all).pack(side="left", padx=4)
        _btn(btn_row, "Refresh",         C["blue"],   self._refresh).pack(side="left", padx=4)

        self._status = tk.Label(self, text="", bg=C["bg"], fg=C["sub"],
                                font=("Segoe UI", 8))
        self._status.pack(pady=(0, 6))

        self._mem_ids: list[int] = []
        self._refresh()

    def _refresh(self):
        self._lb.delete(0, "end")
        self._mem_ids = []
        mems = _memory.get_all()
        if not mems:
            self._lb.insert("end", "  (no memories stored)")
        else:
            for m in mems:
                line = f"  [{m['id']}] {m['category']:<12}  {m['fact'][:44]}"
                if len(m["fact"]) > 44:
                    line += "…"
                self._lb.insert("end", line)
                self._mem_ids.append(m["id"])
        self._status.config(text=f"{len(mems)} memor{'y' if len(mems)==1 else 'ies'} stored")

    def _delete_selected(self):
        sel = self._lb.curselection()
        if not sel:
            self._status.config(text="Select a memory first.")
            return
        idx = sel[0]
        if idx >= len(self._mem_ids):
            return
        mem_id = self._mem_ids[idx]
        result = _memory.forget(mem_id)
        if "error" in result:
            self._status.config(text=result["error"])
        else:
            self._status.config(text=f"Deleted memory [{mem_id}].")
            self._refresh()

    def _clear_all(self):
        count = len(_memory.get_all())
        if count == 0:
            self._status.config(text="Nothing to clear.")
            return
        if not mb.askyesno(
            "Clear all memories",
            f"Delete all {count} memories permanently?\n\nThis cannot be undone.",
            parent=self,
        ):
            return
        _memory.clear_all()
        self._status.config(text="All memories cleared.")
        self._refresh()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sep(parent):
    tk.Frame(parent, bg=C["surface"], height=1).pack(fill="x")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    admin = is_admin()
    if not admin:
        # Offer elevation before opening the main window.
        _root = tk.Tk()
        _root.withdraw()
        want_elev = mb.askyesno(
            "Administrator Required",
            "Power profile management requires Administrator rights.\n\n"
            "Restart as Administrator?\n\n"
            "Click No to run without power management.",
            icon="warning",
        )
        _root.destroy()
        if want_elev:
            # ShellExecuteW returns >32 on success (the OS handles the UAC prompt).
            ret = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable, f'"{__file__}"', None, 1
            )
            if ret > 32:
                sys.exit(0)
            # UAC was denied or failed — fall through and run unelevated.

    app = AssistantApp(admin=is_admin())
    app.mainloop()
