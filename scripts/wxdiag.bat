@echo off
rem WeChat connection check: version, data folder, keys, can it read the groups, windows
cd /d "%~dp0.."
call "%~dp0env.bat" || exit /b 1
python run.py wxdiag
echo.
if /i not "%~1"=="nopause" pause
