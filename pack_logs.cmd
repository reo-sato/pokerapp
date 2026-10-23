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
rem Pack the latest test sessions (last 12 hours) into one zip on the Desktop for review.
rem Extra options: --audio / --no-audio / --text / --hours 48 / --session <id>
"venv\Scripts\python.exe" tools\pack_logs.py %*
pause
