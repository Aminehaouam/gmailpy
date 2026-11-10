@echo off
REM One-click installer: installs Python (if needed), all libraries and shortcuts.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
if errorlevel 1 pause
