@echo off
setlocal
rem Poker Hand Logger - update from GitHub (keeps config.json / data / rfid_cards.json).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\install.ps1" -Update -SkipModel %*
set RC=%ERRORLEVEL%
echo.
if "%RC%"=="0" (echo [update] OK) else (echo [update] FAILED - see install.log)
pause
exit /b %RC%
