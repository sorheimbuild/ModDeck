@echo off
rem ModDeck launcher - starts the local server and opens the app window.
setlocal
cd /d "%~dp0"

set "PYW="
where pythonw.exe >nul 2>nul && set "PYW=pythonw.exe"
if not defined PYW (
  where pyw.exe >nul 2>nul && set "PYW=pyw.exe"
)
if defined PYW (
  start "" %PYW% "%~dp0server.py"
  exit /b 0
)

where python.exe >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Python 3.11 or newer is required.
  echo   Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

echo Starting ModDeck in this window (debug mode)...
python.exe "%~dp0server.py"
pause
