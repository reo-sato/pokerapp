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
rem Ground truth entry (true action history per hand) for iPad / phone / this PC.
rem Open http://<this PC's IPv4>:8791/ on the shop Wi-Fi.
echo [ground-truth] This PC's IPv4 addresses (use one of them on the iPad):
ipconfig | findstr /i "IPv4"
"venv\Scripts\python.exe" tools\ground_truth_ui.py --host 0.0.0.0 --port 8791
pause
