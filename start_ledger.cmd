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
rem Ledger window (+ viewer API for phones / staff iPad when config.json viewer_api.enabled = true).
echo [ledger] Phone / iPad access needs config.json: viewer_api.enabled=true and bind_host=0.0.0.0
"venv\Scripts\python.exe" main.py --ledger --log-file
pause
