@echo off
rem xiaoman-wechat one-click installer (Windows). Double-click it once.
rem  - Uses conda env "ailife" if you already have one; otherwise makes .venv here
rem  - No Python? It installs Python 3.11 with winget (per-user, no admin)
rem  - Installs packages (Tsinghua mirror first), ffmpeg, then runs the setup wizard
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set "MIRROR=-i https://pypi.tuna.tsinghua.edu.cn/simple"
echo ==== xiaoman-wechat installer ====

rem ---------- 1. Python ----------
if exist ".venv\Scripts\python.exe" goto :have_venv

set "ROOTC="
for %%D in ("%USERPROFILE%\anaconda3" "%USERPROFILE%\miniconda3" "C:\ProgramData\anaconda3" "C:\ProgramData\miniconda3" "D:\anaconda3" "D:\Anaconda3" "D:\miniconda3") do (
  if not defined ROOTC if exist "%%~D\Scripts\activate.bat" set "ROOTC=%%~D"
)
if defined ROOTC (
  call "%ROOTC%\Scripts\activate.bat"
  call conda env list | findstr /b /c:"ailife " >NUL
  if not errorlevel 1 goto :use_conda
)

echo [1/4] Looking for Python 3.10+ ...
set "PY="
for %%V in (3.12 3.11 3.10 3.13) do (
  if not defined PY (
    py -%%V -c "import sys" >NUL 2>NUL
    if not errorlevel 1 set "PY=py -%%V"
  )
)
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >NUL 2>NUL
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY="%LOCALAPPDATA%\Programs\Python\Python311\python.exe""
)
if not defined PY (
  echo     No Python found. Installing Python 3.11 with winget ...
  winget install -e --id Python.Python.3.11 --scope user --silent --accept-source-agreements --accept-package-agreements
  if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY="%LOCALAPPDATA%\Programs\Python\Python311\python.exe""
)
if not defined PY (
  echo [ERROR] Could not install Python automatically.
  echo         Install Python 3.11 from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^),
  echo         or from the mirror https://registry.npmmirror.com/binary.html?path=python/3.11.9/
  echo         then double-click install.bat again.
  pause
  exit /b 1
)
echo     Using %PY%
echo [1/4] Creating .venv ...
%PY% -m venv .venv
if errorlevel 1 (
  echo [ERROR] Could not create .venv
  pause
  exit /b 1
)

:have_venv
call ".venv\Scripts\activate.bat"
goto :packages

:use_conda
echo [1/4] Using conda env "ailife"
call conda activate ailife

rem ---------- 2. Packages ----------
:packages
echo [2/4] Installing packages ...
python -m pip install -U pip %MIRROR% >NUL 2>NUL
pip install -r requirements.txt %MIRROR%
if errorlevel 1 (
  echo     Mirror failed, trying the default index ...
  pip install -r requirements.txt
)
if errorlevel 1 (
  echo [ERROR] pip install failed. Check your network / proxy, then run install.bat again.
  pause
  exit /b 1
)
echo     Optional packages ^(lunar calendar^). Errors here are OK ...
pip install lunar_python %MIRROR%
pip install trafilatura %MIRROR%
pip install curl_cffi %MIRROR%

rem ---------- 3. ffmpeg ----------
echo [3/4] Checking ffmpeg ^(used to look at GIFs / videos you send^) ...
where ffmpeg >NUL 2>NUL
if errorlevel 1 winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements

rem ---------- 4. Setup wizard ----------
echo [4/4] Setup wizard ...
if not exist "home\wechat\config.yaml" (
  python run.py setup
) else (
  echo     home\wechat\config.yaml already exists ^(run setup.bat to redo it^).
)
echo.
echo ==== Done! Double-click start.bat to wake her up. ====
pause
