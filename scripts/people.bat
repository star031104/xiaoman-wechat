@echo off
rem Show how she sees each person in the group (profiles)
cd /d "%~dp0.."
call "%~dp0env.bat" || exit /b 1
python run.py people
echo.
if /i not "%~1"=="nopause" pause
