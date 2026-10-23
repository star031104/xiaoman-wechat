@echo off
rem Add / switch your LLM API (any OpenAI-compatible URL + key)
cd /d "%~dp0.."
call "%~dp0env.bat" || exit /b 1
python run.py llm
echo.
if /i not "%~1"=="nopause" pause
