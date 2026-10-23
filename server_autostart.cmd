@echo off
setlocal
cd /d "%~dp0"
rem Store server (main.py --serve, ADR-0065): API for phones and the staff iPad (/staff/) + table monitor.
rem Registers it to start when this user signs in (the shop PC signs in automatically) and to restart
rem within 5 minutes if it stops. No administrator permission needed. "server_autostart.cmd remove" unregisters it.
if not exist "venv\Scripts\pythonw.exe" (
  echo [error] venv not found. Run install.cmd first.
  pause
  exit /b 1
)
if /i "%~1"=="remove" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\store_server_task.ps1" -Remove
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\store_server_task.ps1"
)
