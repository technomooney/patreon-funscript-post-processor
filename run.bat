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
echo   2^) Download content       -- find links in description.json files
echo      and download the associated videos
echo.
echo   3^) Check funscript match  -- find videos missing a funscript and
echo      report fuzzy-match suggestions
echo.
echo   4^) Generate HTML          -- build a description.html visual overview
echo      in each post folder
echo.
echo   5^) Sync new folders       -- copy folders that are new in the Patreon
echo      downloader output into the post-processor working directory
echo.

:ask
set /p "choice=Choose a program to run (1-5): "

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
    .venv\Scripts\python.exe check_funscripts.py
    goto done
)
if "%choice%"=="4" (
    echo.
    .venv\Scripts\python.exe generate_html.py
    goto done
)
if "%choice%"=="5" (
    echo.
    .venv\Scripts\python.exe sync_new_folders.py
    goto done
)

echo Invalid choice. Please enter 1, 2, 3, 4 or 5.
goto ask

:done
echo.
pause
