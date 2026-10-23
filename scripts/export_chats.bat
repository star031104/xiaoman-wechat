@echo off
rem Export chat history of a few groups (read-only) to home\wechat\data\export
cd /d "%~dp0.."
call "%~dp0env.bat" || exit /b 1
python run.py wxexport
echo.
if /i not "%~1"=="nopause" pause
