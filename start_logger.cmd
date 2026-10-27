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
rem Ground-truth entry for this PC (the logger opens http://127.0.0.1:8791/table after the session starts).
rem Start it in another window when nothing is listening on 8791 yet (it keeps running after the logger closes).
netstat -an | findstr /c:"0.0.0.0:8791 " >nul
if errorlevel 1 (
  echo [logger] Starting the ground-truth server in another window. Keep it open.
  start "ground-truth" "%~dp0start_truth.cmd"
)
"venv\Scripts\python.exe" main.py --cli --log-file
pause
