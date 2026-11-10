@echo off
REM Starts Bewerbung Attach Assistant from the project's virtual environment
REM without a console window. Add --dry-run to lock the preview-only mode:
REM     start.bat --dry-run
cd /d "%~dp0"
if not exist .venv\Scripts\pythonw.exe (
    echo The virtual environment .venv was not found. See README.md, "Installation".
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" main.py %*
