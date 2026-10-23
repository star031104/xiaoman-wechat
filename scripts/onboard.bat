@echo off
rem Before she joins: export old chat history, let the LLM write a "who is who" draft, review it, load it
cd /d "%~dp0.."
call "%~dp0env.bat" || exit /b 1
python run.py onboard
echo.
pause
