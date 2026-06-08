@echo off
:: Preferred launcher: use the "Assistant Toggle" desktop shortcut (no console window).
:: This .bat is kept for terminal use (debugging). Double-clicking it flashes a
:: brief console; use the .lnk shortcut on the desktop to avoid that.
cd /d "%~dp0"
start "" /b wscript.exe //nologo "%~dp0assistant_toggle.vbs"
