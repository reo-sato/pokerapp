@echo off
setlocal
rem Poker Hand Logger - remove shortcuts and venv (data files are kept).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\install.ps1" -Uninstall %*
set RC=%ERRORLEVEL%
echo.
pause
exit /b %RC%
