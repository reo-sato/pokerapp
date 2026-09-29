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
rem Card check with RFID (seats 4, 5, 6; first button at seat 6). No table setup to type.
rem The steps are on the iPad: http://<this PC's IPv4>:8791/script (start_truth.cmd).
"venv\Scripts\python.exe" main.py --cli --log-file --script cards
pause
