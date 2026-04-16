@echo off
cd /d "%~dp0"

if not exist ".venv" (
    echo Virtual environment not found. Run setup.bat first.
    pause
    exit /b 1
)

if not exist ".env" (
    echo .env file not found. Run setup.bat first.
    pause
    exit /b 1
)

:: --- Program selection ------------------------------------------------------

echo.
echo ========================================
echo   Patreon Downloader Post-Processor
echo ========================================
echo.
echo   1^) Fix file prefixes -- strip the attachment ID prefix from
echo      downloaded filenames (run this first)
echo.
echo   2^) Download content  -- find links in description.json files
echo      and download the associated videos
echo.
echo   3^) Generate HTML     -- build an index.html visual overview
echo      from description.json files
echo.

:ask
set /p "choice=Choose a program to run (1, 2 or 3): "

if "%choice%"=="1" (
    echo.
    .venv\Scripts\python.exe prefixFix.py
    goto done
)
if "%choice%"=="2" (
    echo.
    .venv\Scripts\python.exe downloadContent.py
    goto done
)
if "%choice%"=="3" (
    echo.
    .venv\Scripts\python.exe generate_html.py
    goto done
)

echo Invalid choice. Please enter 1, 2 or 3.
goto ask

:done
echo.
pause
