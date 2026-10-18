@echo off
rem Convert existing libraries to the shared library database. Dry run unless --apply is given.
rem Usage: migrate.bat D:\Patreon [more roots] [--apply] [--report file.json]
cd /d "%~dp0"
if not exist ".venv" (
    echo Virtual environment not found. Run setup.bat first.
    exit /b 1
)
.venv\Scripts\python.exe scripts\migrate_library_data.py %*
