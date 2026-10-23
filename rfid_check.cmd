@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
if not exist "venv\Scripts\python.exe" (
  echo [error] venv not found. Run install.cmd first.
  pause
  exit /b 1
)
rem RFID reader check (PC/SC): lists readers, then lints config.json and connects to each reader.
"venv\Scripts\python.exe" tools\probe_pcsc.py list
"venv\Scripts\python.exe" tools\probe_pcsc.py check
pause
