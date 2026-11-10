@echo off
setlocal
cd /d "%~dp0"

echo.
echo  ModDeck - build the standalone .exe
echo  ===================================
echo.

if not exist ".build-venv\Scripts\python.exe" (
  echo [1/4] Creating build virtual environment...
  python -m venv .build-venv
  if errorlevel 1 goto :fail
) else (
  echo [1/4] Reusing .build-venv
)

set "PY=.build-venv\Scripts\python.exe"

echo [2/4] Installing build tools (pywebview, pyinstaller)...
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install --upgrade pywebview pyinstaller
if errorlevel 1 goto :fail

if not exist "web\icon.ico" (
  echo [3/4] Generating icon...
  "%PY%" tools\make_icon.py
  if errorlevel 1 goto :fail
) else (
  echo [3/4] Using web\icon.ico
)

echo [4/4] Packaging dist\ModDeck.exe ...
if exist "build" rmdir /s /q "build"
if not exist "dist" mkdir "dist"
"%PY%" -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name ModDeck ^
  --icon "web\icon.ico" ^
  --add-data "web;web" ^
  --collect-all webview ^
  --hidden-import webview.platforms.edgechromium ^
  --hidden-import webview.platforms.winforms ^
  desktop.py
if errorlevel 1 goto :fail

echo.
echo Done. Run dist\ModDeck.exe
echo Your mods and settings live in %%LOCALAPPDATA%%\ModDeck when using the exe.
exit /b 0

:fail
echo.
echo Build failed. Scroll up for the error.
exit /b 1
