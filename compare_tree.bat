@echo off
rem Compare a library with the tree snapshot saved by reset.bat; optionally restore old names (see scripts\compare_tree.py).
rem   compare_tree.bat D:\Patreon [--creator X] [--only TEXT] [--restore-names [--apply]]
cd /d "%~dp0"
if not exist ".venv" (
    echo Virtual environment not found. Run setup.bat first.
    exit /b 1
)
.venv\Scripts\python.exe scripts\compare_tree.py %*
