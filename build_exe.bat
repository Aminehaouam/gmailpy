@echo off
REM Builds dist\BewerbungAttachAssistant.exe (single file, no console window).
REM Run from the project folder. Creates/uses the virtual environment .venv.

setlocal
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
    echo Creating virtual environment...
    py -3 -m venv .venv || goto :error
)

call .venv\Scripts\activate.bat || goto :error
python -m pip install --upgrade pip || goto :error
pip install -r requirements-dev.txt || goto :error

echo Running tests...
python -m pytest -q || goto :error

echo Building executable...
pyinstaller --noconfirm --clean BewerbungAttachAssistant.spec || goto :error

echo Checking the executable (a window with the self-test result opens)...
start "" /wait dist\BewerbungAttachAssistant.exe --self-test
if errorlevel 1 goto :error

echo.
echo Done: %~dp0dist\BewerbungAttachAssistant.exe
goto :eof

:error
echo.
echo Build FAILED (see messages above).
exit /b 1
