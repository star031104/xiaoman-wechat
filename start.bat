@echo off
rem Start her in the background with a tray icon (bottom-right of the taskbar), no console window.
rem Right-click the icon: status, open her home page, log, restart, autostart, quit. Debugging: scripts\start_console.bat
cd /d "%~dp0"
if not exist "home\wechat\config.yaml" (
  echo Not set up yet. Double-click install.bat ^(or setup.bat^) first.
  pause
  exit /b 1
)
call "%~dp0scripts\env.bat" || exit /b 1

python -c "import pystray, PIL" >NUL 2>NUL
if errorlevel 1 (
  echo First run: installing the tray icon packages ^(pystray, Pillow^) ...
  python -m pip install pystray Pillow -i https://pypi.tuna.tsinghua.edu.cn/simple
  if errorlevel 1 python -m pip install pystray Pillow
)
python -c "import pystray, PIL" >NUL 2>NUL
if errorlevel 1 goto console
where pythonw >NUL 2>NUL
if errorlevel 1 goto console
start "" pythonw "%~dp0scripts\tray.pyw"
exit /b 0

:console
echo Tray icon is not available, running in this window instead.
call "%~dp0scripts\start_console.bat"
