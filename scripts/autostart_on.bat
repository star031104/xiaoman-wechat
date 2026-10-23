@echo off
REM Start her when you log in to Windows (no admin needed, no console window)
set "SU=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:SU+'\xiaoman-wechat.lnk'); $s.TargetPath='wscript.exe'; $s.Arguments='\"%~dp0..\start.vbs\"'; $s.WorkingDirectory='%~dp0..'; $s.Save()"
if exist "%SU%\xiaoman-wechat.lnk" (
  echo Autostart is ON. It starts when you log in to Windows.
) else (
  echo Could not create the shortcut. Put a shortcut to start.vbs into: %SU%
)
echo (To turn it off: scripts\autostart_off.bat)
if /i not "%~1"=="nopause" pause
