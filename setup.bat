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

set "EXISTING_HEADLESS=false"
set "EXISTING_RES=1080"
set "EXISTING_IWARA_SECRET=5nFp9kmbNnHdAFhaqMvt"

if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        if "%%A"=="BROWSER_HEADLESS"    set "EXISTING_HEADLESS=%%B"
        if "%%A"=="MAX_RESOLUTION"      set "EXISTING_RES=%%B"
        if "%%A"=="IWARA_SECRET"        set "EXISTING_IWARA_SECRET=%%B"
    )
)

:: --- Prompt for settings (non-sensitive only) --------------------------------

echo.
echo ========================================
echo   Configure settings
echo   Press Enter to keep the current value
echo ========================================
echo.

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

:: IWARA_SECRET
echo.
echo   iwara.tv signing secret -- only change this if downloads start
echo   failing with 403 errors. To find the new value, open iwara.tv
echo   in your browser, go to DevTools ^> Network, watch the CDN download
echo   request, and read the X-Version header from its request headers.
echo.
set "IWARA_SECRET=!EXISTING_IWARA_SECRET!"
set /p "IWARA_SECRET=iwara.tv signing secret [!EXISTING_IWARA_SECRET!]: "

:: --- Write .env (credentials are stored in the OS keyring, not here) --------

(
    echo # Run the browser in headless mode ^(no visible window^).
    echo # Set to false if sites start blocking the automation.
    echo BROWSER_HEADLESS=!BROWSER_HEADLESS!
    echo.
    echo # Maximum resolution to download ^(e.g. 1080, 720, 2160^).
    echo # Downloads the highest quality available up to this value.
    echo MAX_RESOLUTION=!MAX_RESOLUTION!
    echo.
    echo # iwara.tv CDN signing secret -- embedded in the iwara.tv frontend JS.
    echo # If downloads return 403, find the new value via DevTools Network tab:
    echo # watch the CDN download request and read the X-Version request header.
    echo IWARA_SECRET=!IWARA_SECRET!
) > .env

echo.
echo .env written.

:: --- Credentials (stored securely in the OS keyring) ------------------------

echo.
echo ========================================
echo   Credential Setup
echo ========================================
.venv\Scripts\python.exe setup_credentials.py

echo ========================================
echo   Setup complete!
echo   Run run.bat to start the downloader.
echo ========================================
echo.
pause
