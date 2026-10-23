@echo off
rem Setup wizard (run it again any time to change only what you want)
cd /d "%~dp0"
call "%~dp0scripts\env.bat" || exit /b 1
python run.py setup %1 %2
echo.
pause
