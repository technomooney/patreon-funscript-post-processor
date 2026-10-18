@echo off
rem Reset a library for a fresh download, keeping a tree snapshot and leaving .manual folders alone. Two steps, outside the menu:
rem   reset.bat D:\Patreon                    plan + HTML report + confirmation token (changes nothing)
rem   reset.bat D:\Patreon --confirm TOKEN    apply (resumable)
cd /d "%~dp0"
if not exist ".venv" (
    echo Virtual environment not found. Run setup.bat first.
    exit /b 1
)
.venv\Scripts\python.exe scripts\reset_library.py %*
