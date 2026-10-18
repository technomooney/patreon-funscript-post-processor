@echo off
rem Report differences between a library on disk and its database (read-only unless --accept is used).
rem Usage: check_drift.bat D:\Patreon [--csv out.csv] [--accept ID] [--accept-all KIND]
cd /d "%~dp0"
if not exist ".venv" (
    echo Virtual environment not found. Run setup.bat first.
    exit /b 1
)
.venv\Scripts\python.exe scripts\check_library_drift.py %*
