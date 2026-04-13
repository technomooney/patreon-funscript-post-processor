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
echo   1^) Download content  -- find links in description.json files
echo      and download the associated videos
echo.
echo   2^) Fix file prefixes -- strip the attachment ID prefix from
echo      downloaded filenames
echo.

:ask
set /p "choice=Choose a program to run (1 or 2): "

if "%choice%"=="1" (
    echo.
    .venv\Scripts\python.exe downloadContent.py
    goto done
)
if "%choice%"=="2" (
    echo.
    .venv\Scripts\python.exe prefixFix.py
    goto done
)

echo Invalid choice. Please enter 1 or 2.
goto ask

:done
echo.
pause
