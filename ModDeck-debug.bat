@echo off
rem Same as ModDeck.bat but keeps the console open so you can see errors and the log.
setlocal
cd /d "%~dp0"
python.exe "%~dp0server.py" %*
echo.
echo ModDeck stopped.
pause
