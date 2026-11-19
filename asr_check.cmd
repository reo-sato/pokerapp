@echo off
setlocal
cd /d "%~dp0"
rem Transcription API check (ADR-0072). Double-click = check (key, settings, sends 3 saved utterances).
rem "asr_check.cmd set-key" stores the OpenAI key (hidden input), "asr_check.cmd use openai" / "use local"
rem switches the logger, "asr_check.cmd remove-key" deletes the key. Steps: docs\whisper-api-todo.md
if not exist "venv\Scripts\python.exe" (
  echo [error] venv not found. Run install.cmd first.
  pause
  exit /b 1
)
"venv\Scripts\python.exe" tools\asr_check.py %*
pause
