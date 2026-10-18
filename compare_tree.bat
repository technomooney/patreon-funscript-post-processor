@echo off
rem Compare a library with the tree snapshot saved by reset.bat; optionally restore old names (see scripts\compare_tree.py).
rem   compare_tree.bat D:\Patreon [--creator X] [--only TEXT] [--restore-names [--apply]]
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
    .venv\Scripts\python.exe scripts\compare_tree.py %FOLDER%
) else (
    .venv\Scripts\python.exe scripts\compare_tree.py %*
)
if defined ASKED pause
