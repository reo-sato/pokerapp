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
rem Table monitor for iPad / phone on the shop Wi-Fi. Open http://<this PC's IPv4>:8790/
echo [monitor] This PC's IPv4 addresses (use one of them on the iPad):
ipconfig | findstr /i "IPv4"
"venv\Scripts\python.exe" tools\table_monitor.py --host 0.0.0.0 --port 8790
pause
