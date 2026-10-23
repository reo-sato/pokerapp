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
rem The script page is served by the ground-truth server (start_truth.cmd, port 8791).
rem Start it in another window when nothing is listening on 8791 yet (store 2026-09-30:
rem only the logger was started, and the script page could not load).
netstat -an | findstr /c:"0.0.0.0:8791 " >nul
if errorlevel 1 (
  echo [script] Starting the ground-truth server in another window. Keep it open.
  start "ground-truth" "%~dp0start_truth.cmd"
)
"venv\Scripts\python.exe" main.py --cli --log-file --script cards
pause
