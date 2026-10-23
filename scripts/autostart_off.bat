@echo off
set "SU=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
del "%SU%\xiaoman-wechat.lnk" 2>NUL
echo Autostart is OFF.
if /i not "%~1"=="nopause" pause
