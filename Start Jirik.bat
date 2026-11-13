@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Záložní spuštění bez aplikace: chat se otevře v prohlížeči.
if not exist ".venv\Scripts\python.exe" call "%~dp0Setup.bat"
".venv\Scripts\python.exe" run.py
pause
