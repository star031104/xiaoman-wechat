@echo off
rem Open her "home" (dashboard) when she runs on a Linux server.
rem Forwards the server's local-only ports to this PC through SSH, then opens the browser:
rem   8768 = her home page   5800 = WeChat web VNC (scan to log in)
rem First run asks for the server (like ubuntu@1.2.3.4) and saves it to data\server.txt.
rem An SSH key at data\server_key is used if present (otherwise your normal ~/.ssh keys / password).
setlocal
cd /d "%~dp0.."
if not exist data mkdir data
if not exist data\server.txt (
  set /p SRV=Server, like ubuntu@1.2.3.4 : 
  call echo %%SRV%%> data\server.txt
)
set /p SRV=<data\server.txt
set "KEY="
if exist data\server_key (
  icacls "%CD%\data\server_key" /inheritance:r /grant:r "%USERNAME%":R >NUL 2>NUL
  set KEY=-i "%CD%\data\server_key"
)
where ssh >NUL 2>NUL || (echo [ERROR] ssh not found. Windows 10/11: Settings - Apps - Optional features - OpenSSH Client & pause & exit /b 1)
echo Connecting to %SRV% ...  (keep the small window open; close it to disconnect)
start "xiaoman tunnel" /min ssh %KEY% -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes -o ServerAliveInterval=30 -o ExitOnForwardFailure=no -N -L 8768:127.0.0.1:8768 -L 5800:127.0.0.1:5800 %SRV%
timeout /t 4 /nobreak >NUL
set "PAGE=%~1"
if "%PAGE%"=="" set "PAGE=8768"
start "" http://127.0.0.1:%PAGE%
endlocal
