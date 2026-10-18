@echo off
rem Rebuild a library's pixeldrain-sourced files from their sources. Two steps, deliberately outside the menu:
rem   rebuild.bat D:\Patreon                                   plan + HTML report + confirmation token
rem   rebuild.bat D:\Patreon --confirm TOKEN [--decisions f]   apply (resumable)
cd /d "%~dp0"
if not exist ".venv" (
    echo Virtual environment not found. Run setup.bat first.
    exit /b 1
)
.venv\Scripts\python.exe scripts\rebuild_library.py %*
