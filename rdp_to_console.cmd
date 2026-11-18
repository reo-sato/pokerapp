@echo off
setlocal
rem Return this Remote Desktop session to the PC's console instead of closing it with the X button
rem (ADR-0073 stage 0). A closed (disconnected) session may lose the microphone; a session moved to the
rem console keeps running as if someone were signed in at the PC. Asks for administrator permission once.
rem The Remote Desktop window disconnects right after this.
powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -Command tscon.exe (Get-Process -Id $PID).SessionId /dest:console'"
