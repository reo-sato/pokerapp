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
rem Pack the latest test sessions (last 12 hours) WITH all utterance audio for review.
rem Zips over 25 MB are split into parts (_1of3.zip ...). Attach all parts.
rem Extra options: --hours 48 / --session <id> / --part-mb 25
"venv\Scripts\python.exe" tools\pack_logs.py --audio %*
pause
