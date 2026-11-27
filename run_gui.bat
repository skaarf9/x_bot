@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=pythonw"
if exist "%~dp0.venv\Scripts\pythonw.exe" set "PY=%~dp0.venv\Scripts\pythonw.exe"
start "" "%PY%" "%~dp0gui.py"
