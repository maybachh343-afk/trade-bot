@echo off
setlocal
cd /d "%~dp0"
py -3.12 -c "import sys" >nul 2>&1
if errorlevel 1 (
  echo Install Python 3.12 for Windows from python.org, then run this file again.
  echo Keep the Python launcher and Tcl/Tk options selected during installation.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" py -3.12 -m venv .venv
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" windows_host.py setup
if errorlevel 1 goto failed
exit /b 0
:failed
echo Setup did not finish. Keep this window open to review the error.
pause
exit /b 1
