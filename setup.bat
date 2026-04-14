@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ========================================
echo   Patreon Downloader - Setup
echo ========================================
echo.

:: --- Virtual environment ----------------------------------------------------

if exist ".venv" (
    echo Virtual environment already exists, skipping creation.
) else (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo ERROR: Failed to create virtual environment.
        echo Make sure Python 3 is installed and on your PATH.
        pause
        exit /b 1
    )
    echo Virtual environment created.
)

echo Installing dependencies...
.venv\Scripts\pip install --quiet --upgrade pip
.venv\Scripts\pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo.
    echo ERROR: Failed to install dependencies.
    pause
    exit /b 1
)
echo Updating yt-dlp to latest version...
.venv\Scripts\pip install --quiet --upgrade yt-dlp
echo Dependencies installed.

:: --- Settings and credentials ------------------------------------------------

.venv\Scripts\python.exe setup_credentials.py

echo ========================================
echo   Setup complete!
echo   Run run.bat to start the downloader.
echo ========================================
echo.
pause
