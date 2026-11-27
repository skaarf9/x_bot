@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
"%PY%" "%~dp0x_monitor.py" %*
pause
