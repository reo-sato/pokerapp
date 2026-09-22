@echo off
setlocal
rem Poker Hand Logger - one-step installer (double-click). Japanese messages are inside installer\install.ps1.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\install.ps1" %*
set RC=%ERRORLEVEL%
echo.
if "%RC%"=="0" (echo [install] OK) else (echo [install] FAILED - see install.log)
pause
exit /b %RC%
