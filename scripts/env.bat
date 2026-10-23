@echo off
rem Activate the Python env:
rem   1) .venv in the project folder (made by install.bat when there is no conda)
rem   2) conda env "ailife" (older installs)
set "AILIFE_ENV="
if exist "%~dp0..\.venv\Scripts\python.exe" (
  call "%~dp0..\.venv\Scripts\activate.bat"
  set "AILIFE_ENV=venv"
  goto :done
)
set "ACT="
for %%D in ("%USERPROFILE%\anaconda3" "%USERPROFILE%\miniconda3" "C:\ProgramData\anaconda3" "C:\ProgramData\miniconda3" "D:\anaconda3" "D:\Anaconda3" "D:\miniconda3") do (
  if not defined ACT if exist "%%~D\Scripts\activate.bat" set "ACT=%%~D\Scripts\activate.bat"
)
if defined ACT (
  call "%ACT%" ailife
) else (
  call conda activate ailife 2>NUL
)
set "AILIFE_ENV=conda"
:done
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set "CHECK=import yaml, openai, aiohttp, cryptography, zstandard"
python -c "%CHECK%" >NUL 2>NUL
if errorlevel 1 (
  echo [ERROR] Python packages are missing. Double-click install.bat first.
  pause
  exit /b 1
)
exit /b 0
