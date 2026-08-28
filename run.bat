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
        echo   [deps] Dependencies are !DEPS_AGE_DAYS! day^(s^) old -- choose 'ru' below to update.
        echo.
    )
) else (
    echo   [deps] Dependency update status unknown -- choose 'ru' below to update.
    echo.
)
echo   1^) Sync new folders       -- copy folders that are new in the Patreon
echo      downloader output into the post-processor working directory, then
echo      optionally check existing folders for files missing by content
echo      (e.g. new funscripts added after the folder was first copied)
echo      (run this first)
echo.
echo   2^) Fix file prefixes      -- strip the attachment ID prefix from
echo      downloaded filenames
echo.
echo   3^) Download content       -- find links in description.json files
echo      and download the associated videos and files
echo.
echo   4^) Extract variant archives -- for creators (e.g. Pize) who now ship scripts
echo      only inside a password-protected .rar/.zip/.7z per intensity variant:
echo      extracts each archive and renames its funscripts with the variant folded
echo      in, so they don't collide. Password resolved from local history first,
echo      falling back to a live Discord fetch -- first time for a creator, it
echo      asks inline for the Discord channel to use, no separate setup needed.
echo.
echo   5^) Download from funscript metadata -- for creators who put the source
echo      video's URL in the funscript's own metadata.video_url field instead of
echo      (or in addition to) the post body: download it for any funscript that
echo      has one but no matching video on disk yet. Same download engine as
echo      option 3, tagged separately (own report files, own undo/folder_log entry)
echo      so it never overwrites that run's output.
echo.
echo   6^) Fix garbled names      -- four-pass cleanup pipeline:
echo      * detect video files with wrong/missing extension (magic bytes)
echo      * detect funscripts with wrong/missing .funscript extension
echo      * decode percent-encoded or mojibake filenames
echo      * fuzzy-match funscript names to their video and rename to match
echo      All changes written to CSV reports in _reports/
echo.
echo   7^) Check funscript match  -- find videos missing a funscript and
echo      report fuzzy-match suggestions, cross-checked against video/funscript
echo      duration; can auto-rename a lone unmatched video to its funscript's
echo      name when duration confirms it unambiguously (asks first)
echo.
echo   8^) Dedupe only            -- clean leftover temp files, remove exact
echo      duplicate files (a funscript compares by its points, keeping
echo      whichever copy has richer metadata -- not just whichever is oldest),
echo      then runs pack consolidation (see 'p') over the same folder --
echo      all moved to .trash, undoable, no full download involved
echo.
echo   9^) Generate HTML          -- build a description.html visual overview
echo      in each post folder
echo.
echo   10^) Audit report          -- read .folder_log.json from every post folder
echo      and generate _reports/audit_report.html showing what each script
echo      has done, with per-folder detail and an overall summary
echo.
echo   n^) Creator naming scripts -- submenu of one-creator naming-quirk fixes
echo      (e.g. MDemaxis's SMOOTH-prefix rename). Drop your own .py file into
echo      scripts\creator_scripts\ to add one without editing this menu --
echo      see scripts\creator_scripts\README.md for the contract.
echo.
echo   p^) Consolidate packs      -- for creators (e.g. Pize) who repost
echo      already-released videos+scripts inside a later collection post:
echo      finds a video sitting with no local funscript next to it whose
echo      script only survives in one of these packs (confirmed by actual
echo      audio/video comparison, not filename), keeps whichever copy fits
echo      MAX_RESOLUTION better in the pack folder, and removes the
echo      redundant copy -- not every creator does this, most won't need it
echo.
echo   sa^) Set up unattended run -- configure which steps run for a creator,
echo      in what order, and every prompt's answer, so 'ra' can replay it
echo      later with zero prompts. Re-run to edit an existing config.
echo.
echo   sc^) Update credentials    -- re-enter any service login/API key (pixeldrain,
echo      iwara.tv, mega.nz, spankbang.com) without re-answering every other setup
echo      question -- run this if a saved credential expires or gets revoked
echo.
echo   ru^) Update dependencies   -- upgrade pip packages in the venv (incl. yt-dlp,
echo      undetected-chromedriver, selenium) -- run this if downloads start failing
echo      after a site or browser update
echo.
echo   rz^) Undo last action      -- reverse the most recent renames/copies/dedupe
echo      from options 1-8, a creator script (n), or consolidate packs (p)
echo      (one level deep -- running any of them again replaces what 'last
echo      action' means)
echo.
echo   ra^) Run unattended        -- replay a creator's saved unattended config
echo      (set up via 'sa') start to finish with no prompts; writes its own
echo      log file under that creator's _reports/ folder
echo.
echo   q^) Exit
echo.

:ask
set /p "choice=Choose a program to run (1-10, n=creator scripts, p=consolidate packs, sa/sc=settings, ru/rz/ra=run, q=exit): "

if /i "%choice%"=="q" goto done
if /i "%choice%"=="ru" (
    echo.
    .venv\Scripts\pip.exe install --quiet --upgrade pip
    .venv\Scripts\python.exe scripts\update_deps.py
    powershell -NoProfile -Command "Get-Date -Format o" > "%DEPS_MARKER%"
    echo Dependencies updated.
    echo.
    pause
    goto menu
)
if /i "%choice%"=="sc" (
    echo.
    .venv\Scripts\python.exe scripts\setup_config.py --credentials
    echo.
    pause
    goto menu
)
if /i "%choice%"=="sa" (
    echo.
    .venv\Scripts\python.exe scripts\setup_unattended.py
    echo.
    pause
    goto menu
)
if /i "%choice%"=="n" (
    echo.
    .venv\Scripts\python.exe scripts\creator_scripts_menu.py
    echo.
    pause
    goto menu
)
if /i "%choice%"=="p" (
    echo.
    .venv\Scripts\python.exe scripts\consolidate_packs.py
    echo.
    pause
    goto menu
)
if /i "%choice%"=="rz" (
    echo.
    .venv\Scripts\python.exe scripts\undo_last_action.py
    echo.
    pause
    goto menu
)
if /i "%choice%"=="ra" (
    echo.
    .venv\Scripts\python.exe scripts\run_unattended.py
    echo.
    pause
    goto menu
)
if "%choice%"=="1" (
    echo.
    .venv\Scripts\python.exe scripts\sync_new_folders.py
    echo.
    pause
    goto menu
)
if "%choice%"=="2" (
    echo.
    .venv\Scripts\python.exe scripts\prefixFix.py
    echo.
    pause
    goto menu
)
if "%choice%"=="3" (
    echo.
    .venv\Scripts\python.exe scripts\downloadContent.py
    echo.
    pause
    goto menu
)
if "%choice%"=="4" (
    echo.
    .venv\Scripts\python.exe scripts\extract_variant_archives.py
    echo.
    pause
    goto menu
)
if "%choice%"=="5" (
    echo.
    .venv\Scripts\python.exe scripts\download_from_funscript_metadata.py
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
    .venv\Scripts\python.exe scripts\check_funscripts.py
    echo.
    pause
    goto menu
)
if "%choice%"=="8" (
    echo.
    .venv\Scripts\python.exe scripts\dedupe_only.py
    echo.
    pause
    goto menu
)
if "%choice%"=="9" (
    echo.
    .venv\Scripts\python.exe scripts\generate_html.py
    echo.
    pause
    goto menu
)
if "%choice%"=="10" (
    echo.
    .venv\Scripts\python.exe scripts\generate_audit_report.py
    echo.
    pause
    goto menu
)

echo Invalid choice. Please enter 1-10, n, p, sa, sc, ru, rz, ra, or q to exit.
goto ask

:done
