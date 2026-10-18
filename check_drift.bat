@echo off
rem Report differences between a library on disk and its database (read-only unless --accept is used).
rem Usage: check_drift.bat D:\Patreon [--csv out.csv] [--accept ID] [--accept-all KIND]
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
    .venv\Scripts\python.exe scripts\check_library_drift.py %FOLDER%
) else (
    .venv\Scripts\python.exe scripts\check_library_drift.py %*
)
if defined ASKED pause
