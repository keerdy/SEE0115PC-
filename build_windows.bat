@echo off
setlocal EnableExtensions
cd /d "%~dp0"

for /f "usebackq tokens=*" %%V in ("VERSION") do set "APP_VERSION=%%V"
if "%APP_VERSION%"=="" (
    echo ERROR: VERSION is empty.
    exit /b 1
)

"%~dp0.venv\Scripts\python.exe" -m pip install -r requirements.txt -r requirements-build.txt || exit /b 1
"%~dp0.venv\Scripts\python.exe" packaging\make_icon.py --svg source\title_photo.svg --output source\PocketTestAgent.ico || exit /b 1
for /d /r "%~dp0" %%d in (__pycache__) do rmdir /s /q "%%d" 2>nul
"%~dp0.venv\Scripts\python.exe" -m PyInstaller --noconfirm --windowed --onedir --name SXPocketTestAgent --icon "source\PocketTestAgent.ico" --add-data "source;source" --add-data "configs;configs" --collect-all PySide6 --collect-all apptest testagent_gui.py || exit /b 1
"%~dp0.venv\Scripts\python.exe" packaging\verify_release.py --release-dir "dist\SXPocketTestAgent\_internal" || exit /b 1

where ISCC >nul 2>nul
if errorlevel 1 (
    echo ERROR: Inno Setup 6 is required. Install it and add ISCC.exe to PATH.
    exit /b 1
)

set "POCKET_TESTAGENT_VERSION=%APP_VERSION%"
ISCC packaging\PocketTestAgent.iss || exit /b 1
echo.
echo Done: installer\SXPocketTestAgent-%APP_VERSION%-Setup.exe
