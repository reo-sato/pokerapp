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
rem Store server in this window (for checking). Normally it runs by itself: see server_autostart.cmd.
rem Phones: http://<this PC's IPv4>:8788/   Staff iPad: http://<this PC's IPv4>:8788/staff/   Log: logs\store_server.log
ipconfig | findstr /i "IPv4"
"venv\Scripts\python.exe" main.py --serve
pause
