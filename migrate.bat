@echo off
rem Convert existing libraries to the shared library database. Dry run unless --apply is given.
rem Usage: migrate.bat D:\Patreon [more roots] [--apply] [--report file.json]
cd /d "%~dp0"
if not exist ".venv" (
    echo Virtual environment not found. Run setup.bat first.
    pause
    exit /b 1
)
set ASKED=
if "%~1"=="" (
    set /p FOLDER=Folder ^(drag it here, then Enter^): 
    set ASKED=1
)
if defined ASKED (
    .venv\Scripts\python.exe scripts\migrate_library_data.py %FOLDER%
) else (
    .venv\Scripts\python.exe scripts\migrate_library_data.py %*
)
if defined ASKED pause
