@echo off
setlocal
cd /d "%~dp0"
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONNOUSERSITE=1"
if not exist "%~dp0runtime\python\pythonw.exe" (
  echo Please extract the entire ZIP before starting the app.
  pause
  exit /b 1
)
start "" "%~dp0runtime\python\pythonw.exe" -E -s "%~dp0portable_start.py"
