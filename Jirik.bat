@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  call "%~dp0Setup.bat"
) else if not exist "node_modules\electron\dist\electron.exe" (
  call "%~dp0Setup.bat"
)
if not exist ".venv\Scripts\python.exe" exit /b 1
if not exist "node_modules\electron\dist\electron.exe" exit /b 1
start "" "%~dp0node_modules\electron\dist\electron.exe" "%~dp0."
