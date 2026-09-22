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
rem Hand logger (CLI). Logs go to logs\pokerapp.log; watch the table with start_monitor.cmd.
"venv\Scripts\python.exe" main.py --cli --log-file
pause
