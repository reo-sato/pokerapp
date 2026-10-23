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
rem Hand history for customers' phones on the shop Wi-Fi. Open http://<this PC's IPv4>:8788/
rem The logger records who sits where only when config.json session_layer.enabled = true.
rem Not needed while start_ledger.cmd serves the same screen (viewer_api.enabled = true, port 8788).
echo [viewer] This PC's IPv4 addresses (customers open http://ADDRESS:8788/ on their phones):
ipconfig | findstr /i "IPv4"
"venv\Scripts\python.exe" main.py --viewer-api --host 0.0.0.0 --port 8788
pause
