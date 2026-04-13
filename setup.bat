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
echo Dependencies installed.

:: --- Read existing .env values (used as defaults) ---------------------------

set "EXISTING_KEY="
set "EXISTING_HEADLESS=false"
set "EXISTING_RES=1080"
set "EXISTING_IWARA_EMAIL="
set "EXISTING_IWARA_PASSWORD="

if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        if "%%A"=="PIXELDRAIN_API_KEY"  set "EXISTING_KEY=%%B"
        if "%%A"=="BROWSER_HEADLESS"    set "EXISTING_HEADLESS=%%B"
        if "%%A"=="MAX_RESOLUTION"      set "EXISTING_RES=%%B"
        if "%%A"=="IWARA_EMAIL"         set "EXISTING_IWARA_EMAIL=%%B"
        if "%%A"=="IWARA_PASSWORD"      set "EXISTING_IWARA_PASSWORD=%%B"
    )
)

:: --- Prompt for settings ----------------------------------------------------

echo.
echo ========================================
echo   Configure settings
echo   Press Enter to keep the current value
echo ========================================
echo.

:: PIXELDRAIN_API_KEY
if defined EXISTING_KEY (
    set "PIXELDRAIN_API_KEY=!EXISTING_KEY!"
    set /p "PIXELDRAIN_API_KEY=Pixeldrain API key [!EXISTING_KEY!]: "
) else (
    set "PIXELDRAIN_API_KEY="
    set /p "PIXELDRAIN_API_KEY=Pixeldrain API key (leave blank for anonymous): "
)

:: BROWSER_HEADLESS
set "BROWSER_HEADLESS=!EXISTING_HEADLESS!"
set /p "BROWSER_HEADLESS=Run browser headless? (true/false) [!EXISTING_HEADLESS!]: "
:: Normalise to lowercase true/false
if /i "!BROWSER_HEADLESS!"=="true"  set "BROWSER_HEADLESS=true"
if /i "!BROWSER_HEADLESS!"=="false" set "BROWSER_HEADLESS=false"
if not "!BROWSER_HEADLESS!"=="true" if not "!BROWSER_HEADLESS!"=="false" (
    echo Invalid value, defaulting to false.
    set "BROWSER_HEADLESS=false"
)

:: MAX_RESOLUTION
set "MAX_RESOLUTION=!EXISTING_RES!"
set /p "MAX_RESOLUTION=Maximum download resolution (e.g. 2160, 1080, 720, 480) [!EXISTING_RES!]: "
:: Validate numeric
echo !MAX_RESOLUTION!| findstr /r "^[0-9][0-9]*$" >nul 2>&1
if errorlevel 1 (
    echo Invalid value, defaulting to 1080.
    set "MAX_RESOLUTION=1080"
)

:: IWARA_EMAIL
if defined EXISTING_IWARA_EMAIL (
    set "IWARA_EMAIL=!EXISTING_IWARA_EMAIL!"
    set /p "IWARA_EMAIL=iwara.tv email [!EXISTING_IWARA_EMAIL!]: "
) else (
    set "IWARA_EMAIL="
    set /p "IWARA_EMAIL=iwara.tv email (leave blank to skip iwara.tv downloads): "
)

:: IWARA_PASSWORD (note: cmd has no silent input, password will be visible)
if defined EXISTING_IWARA_PASSWORD (
    set "IWARA_PASSWORD=!EXISTING_IWARA_PASSWORD!"
    set /p "IWARA_PASSWORD=iwara.tv password (press Enter to keep current): "
) else (
    set "IWARA_PASSWORD="
    set /p "IWARA_PASSWORD=iwara.tv password (leave blank to skip iwara.tv downloads): "
)

:: --- Write .env -------------------------------------------------------------

(
    echo # Pixeldrain API key -- found at https://pixeldrain.com/user/api
    echo # Leave blank to download as anonymous ^(public files only^).
    echo PIXELDRAIN_API_KEY=!PIXELDRAIN_API_KEY!
    echo.
    echo # Run the browser in headless mode ^(no visible window^).
    echo # Set to false if sites start blocking the automation.
    echo BROWSER_HEADLESS=!BROWSER_HEADLESS!
    echo.
    echo # Maximum resolution to download ^(e.g. 1080, 720, 2160^).
    echo # Downloads the highest quality available up to this value.
    echo MAX_RESOLUTION=!MAX_RESOLUTION!
    echo.
    echo # iwara.tv account credentials -- required for 18+ content.
    echo # Leave both blank to skip iwara.tv downloads.
    echo IWARA_EMAIL=!IWARA_EMAIL!
    echo IWARA_PASSWORD=!IWARA_PASSWORD!
) > .env

echo.
echo .env written.
echo.
echo ========================================
echo   Setup complete!
echo   Run run.bat to start the downloader.
echo ========================================
echo.
pause
