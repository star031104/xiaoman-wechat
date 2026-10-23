@echo off
rem Check config, WeChat connection and the LLM
cd /d "%~dp0.."
call "%~dp0env.bat" || exit /b 1
python run.py check
echo.
if /i not "%~1"=="nopause" pause
