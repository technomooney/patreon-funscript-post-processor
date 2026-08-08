@echo off
setlocal enabledelayedexpansion
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

set "DEPS_MARKER=.venv\.deps_updated_at"
set "DEPS_MAX_AGE_DAYS=7"

:menu
echo.
echo ========================================
echo   Patreon Downloader Post-Processor
echo ========================================
echo.
if exist "%DEPS_MARKER%" (
    for /f %%D in ('powershell -NoProfile -Command "[math]::Floor(((Get-Date) - (Get-Date (Get-Content '%DEPS_MARKER%'))).TotalDays)"') do set DEPS_AGE_DAYS=%%D
    if !DEPS_AGE_DAYS! GTR %DEPS_MAX_AGE_DAYS% (
        echo   [deps] Dependencies are !DEPS_AGE_DAYS! day^(s^) old -- choose 'u' below to update.
        echo.
    )
) else (
    echo   [deps] Dependency update status unknown -- choose 'u' below to update.
    echo.
)
echo   1^) Fix file prefixes -- strip the attachment ID prefix from
echo      downloaded filenames (run this first)
echo.
echo   2^) Download content       -- find links in description.json files
echo      and download the associated videos and files
echo.
echo   3^) Check funscript match  -- find videos missing a funscript and
echo      report fuzzy-match suggestions
echo.
echo   4^) Generate HTML          -- build a description.html visual overview
echo      in each post folder
echo.
echo   5^) Sync new folders       -- copy folders that are new in the Patreon
echo      downloader output into the post-processor working directory, then
echo      optionally check existing folders for files missing by content
echo      (e.g. new funscripts added after the folder was first copied)
echo.
echo   6^) Fix garbled names      -- four-pass cleanup pipeline:
echo      * detect video files with wrong/missing extension (magic bytes)
echo      * detect funscripts with wrong/missing .funscript extension
echo      * decode percent-encoded or mojibake filenames
echo      * fuzzy-match funscript names to their video and rename to match
echo      All changes written to CSV reports in _reports/
echo.
echo   7^) Dedupe only            -- clean leftover temp files and remove
echo      exact duplicate files without running a full download
echo.
echo   8^) Audit report           -- read .folder_log.json from every post folder
echo      and generate _reports/audit_report.html showing what each script
echo      has done, with per-folder detail and an overall summary
echo.
echo   9^) MDemaxis rename fix    -- MDemaxis patreon only: rename SMOOTH-prefixed
echo      and _maxinterval-suffixed funscripts to variant naming
echo      (e.g. SMOOTH x.funscript -^> x (SMOOTH).funscript)
echo.
echo   u^) Update dependencies    -- upgrade pip packages in the venv (incl. yt-dlp,
echo      undetected-chromedriver, selenium) -- run this if downloads start failing
echo      after a site or browser update
echo.
echo   c^) Update credentials     -- re-enter any service login/API key (pixeldrain,
echo      iwara.tv, mega.nz, spankbang.com) without re-answering every other setup
echo      question -- run this if a saved credential expires or gets revoked
echo.
echo   r^) Undo last action       -- reverse the most recent renames/copies/dedupe
echo      from options 1, 3, 5, 6, 7, or 9 (one level deep -- running any of them
echo      again replaces what 'last action' means)
echo.
echo   q^) Exit
echo.

:ask
set /p "choice=Choose a program to run (1-9, u=update deps, c=update creds, r=undo last, q=exit): "

if /i "%choice%"=="q" goto done
if /i "%choice%"=="u" (
    echo.
    .venv\Scripts\pip.exe install --quiet --upgrade pip
    .venv\Scripts\python.exe scripts\update_deps.py
    powershell -NoProfile -Command "Get-Date -Format o" > "%DEPS_MARKER%"
    echo Dependencies updated.
    echo.
    pause
    goto menu
)
if /i "%choice%"=="c" (
    echo.
    .venv\Scripts\python.exe scripts\setup_config.py --credentials
    echo.
    pause
    goto menu
)
if /i "%choice%"=="r" (
    echo.
    .venv\Scripts\python.exe scripts\undo_last_action.py
    echo.
    pause
    goto menu
)
if "%choice%"=="1" (
    echo.
    .venv\Scripts\python.exe scripts\prefixFix.py
    echo.
    pause
    goto menu
)
if "%choice%"=="2" (
    echo.
    .venv\Scripts\python.exe scripts\downloadContent.py
    echo.
    pause
    goto menu
)
if "%choice%"=="3" (
    echo.
    .venv\Scripts\python.exe scripts\check_funscripts.py
    echo.
    pause
    goto menu
)
if "%choice%"=="4" (
    echo.
    .venv\Scripts\python.exe scripts\generate_html.py
    echo.
    pause
    goto menu
)
if "%choice%"=="5" (
    echo.
    .venv\Scripts\python.exe scripts\sync_new_folders.py
    echo.
    pause
    goto menu
)
if "%choice%"=="6" (
    echo.
    .venv\Scripts\python.exe scripts\fix_garbled_names.py
    echo.
    pause
    goto menu
)
if "%choice%"=="7" (
    echo.
    .venv\Scripts\python.exe scripts\dedupe_only.py
    echo.
    pause
    goto menu
)
if "%choice%"=="8" (
    echo.
    .venv\Scripts\python.exe scripts\generate_audit_report.py
    echo.
    pause
    goto menu
)
if "%choice%"=="9" (
    echo.
    .venv\Scripts\python.exe scripts\MDemaxis_smooth_fix.py
    echo.
    pause
    goto menu
)

echo Invalid choice. Please enter 1-9, u, c, r, or q to exit.
goto ask

:done
