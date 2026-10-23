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
rem Ground truth entry at home: drag the pokerlogs_*.zip files (all parts) onto this file.
rem Without files it looks for the newest pokerlogs_* set on the Desktop and in Downloads.
rem Opens the entry page on this PC only; press Enter in this window when done to pack pokertruth_*.zip on the Desktop.
"venv\Scripts\python.exe" tools\home_truth.py %*
pause
