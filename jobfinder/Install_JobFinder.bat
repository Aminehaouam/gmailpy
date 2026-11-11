@echo off
REM One-click installer for the Ausbildung Job Finder.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_jobfinder.ps1"
if errorlevel 1 pause
