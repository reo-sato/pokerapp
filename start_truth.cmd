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
rem Already running (started by start_script.cmd / start_cards.cmd or earlier): do not start twice.
netstat -an | findstr /c:"0.0.0.0:8791 " >nul
if not errorlevel 1 (
  echo [ground-truth] Already running on port 8791. Open http://this-PC-IPv4:8791/ on the iPad.
  pause
  exit /b 0
)
rem Ground truth entry (true action history per hand) for iPad / phone / this PC.
rem Open http://<this PC's IPv4>:8791/ on the shop Wi-Fi.
echo [ground-truth] This PC's IPv4 addresses (use one of them on the iPad):
ipconfig | findstr /i "IPv4"
"venv\Scripts\python.exe" tools\ground_truth_ui.py --host 0.0.0.0 --port 8791
pause
