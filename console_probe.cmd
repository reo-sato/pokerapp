@echo off
setlocal
cd /d "%~dp0"
rem Microphone check outside RDP (ADR-0073 stage 0): runs tools\console_probe.py as the task PokerConsoleProbe
rem for 60 minutes now and again at the next sign-in. "console_probe.cmd report" shows the summary,
rem "console_probe.cmd remove" deletes the task. Steps: docs\console-probe.md
if not exist "venv\Scripts\pythonw.exe" (
  echo [error] venv not found. Run install.cmd first.
  pause
  exit /b 1
)
if /i "%~1"=="remove" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\console_probe_task.ps1" -Remove
) else if /i "%~1"=="report" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\console_probe_task.ps1" -Report
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\console_probe_task.ps1"
)
