# Bewerbung Attach Assistant - one-click installer for Windows
#
# Installs everything that is needed:
#   1. Python 3.12 (only if no Python 3.11+ with Tkinter is installed)
#   2. the application files (downloaded from GitHub when not run from the
#      project folder)
#   3. a private Python environment with all dependencies
#   4. shortcuts on the Desktop and in the Start menu
#
# Run it again at any time to update the application; settings, Gmail
# authorization and history are kept (they live in %LOCALAPPDATA%).
#
# Usage: double-click Install.bat, or paste into a Command Prompt:
#   powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/Aminehaouam/gmailpy/claude/great-brown-eyx5q1/install.ps1 | iex"

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # makes downloads much faster in Windows PowerShell 5.1
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$AppName      = "Bewerbung Attach Assistant"
$RepoZipUrl   = "https://codeload.github.com/Aminehaouam/gmailpy/zip/refs/heads/claude/great-brown-eyx5q1"
$PythonUrl    = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
$DefaultDir   = Join-Path $env:USERPROFILE "BewerbungAttachAssistant"

function Step($text)  { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Ok($text)    { Write-Host "    $text" -ForegroundColor Green }
function Info($text)  { Write-Host "    $text" }

function Fail($text) {
    Write-Host ""
    Write-Host "INSTALLATION FAILED: $text" -ForegroundColor Red
    Write-Host "Please send a screenshot of this window if you need help."
    Read-Host "Press Enter to close"
    exit 1
}

# --------------------------------------------------------------------------
# Python
# --------------------------------------------------------------------------
function Test-Python($exe, $pyArgs) {
    # True if this interpreter is Python 3.11+ and has Tkinter.
    try {
        $out = & $exe @pyArgs -c "import sys, tkinter; print('OK' if sys.version_info >= (3, 11) else 'OLD')" 2>$null
        return ($LASTEXITCODE -eq 0 -and "$out".Trim() -eq "OK")
    } catch { return $false }
}

function Find-Python {
    $candidates = @(
        @{ Exe = "py"; Args = @("-3.13") },
        @{ Exe = "py"; Args = @("-3.12") },
        @{ Exe = "py"; Args = @("-3.11") },
        @{ Exe = "py"; Args = @("-3") },
        @{ Exe = "python"; Args = @() },
        @{ Exe = (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"); Args = @() },
        @{ Exe = (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"); Args = @() },
        @{ Exe = (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"); Args = @() }
    )
    foreach ($c in $candidates) {
        if (Test-Python $c.Exe $c.Args) {
            # Resolve to the real python.exe so later steps do not depend on PATH.
            $path = & $c.Exe @($c.Args) -c "import sys; print(sys.executable)"
            return "$path".Trim()
        }
    }
    return $null
}

Write-Host "$AppName - installer" -ForegroundColor White
Write-Host "Nothing is sent anywhere; this only installs the program on this PC."

Step "Checking for Python 3.11 or newer"
$python = Find-Python
if ($python) {
    Ok "Found: $python"
} else {
    Info "Python was not found - downloading Python 3.12 from python.org ..."
    $installer = Join-Path $env:TEMP "python-3.12-installer.exe"
    try { Invoke-WebRequest -Uri $PythonUrl -OutFile $installer -UseBasicParsing }
    catch { Fail "Could not download Python ($($_.Exception.Message)). Check the internet connection." }
    Info "Installing Python for the current user (takes about a minute) ..."
    $p = Start-Process -FilePath $installer -Wait -PassThru -ArgumentList @(
        "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_tcltk=1",
        "Include_launcher=1", "Include_test=0", "Shortcuts=0")
    Remove-Item $installer -ErrorAction SilentlyContinue
    if ($p.ExitCode -ne 0) { Fail "The Python installer ended with code $($p.ExitCode)." }
    $python = Find-Python
    if (-not $python) { Fail "Python was installed but cannot be found. Restart the PC and run the installer again." }
    Ok "Installed: $python"
}

# --------------------------------------------------------------------------
# Application files
# --------------------------------------------------------------------------
Step "Getting the application files"
$here = $PSScriptRoot
if ($here -and (Test-Path (Join-Path $here "main.py")) -and (Test-Path (Join-Path $here "requirements.txt"))) {
    $AppDir = $here
    Ok "Using the files in $AppDir"
} else {
    $AppDir = $DefaultDir
    $zip = Join-Path $env:TEMP "bewerbung-attach-assistant.zip"
    $tmp = Join-Path $env:TEMP "bewerbung-attach-assistant-extract"
    try { Invoke-WebRequest -Uri $RepoZipUrl -OutFile $zip -UseBasicParsing }
    catch { Fail "Could not download the application ($($_.Exception.Message))." }
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    $src = Get-ChildItem $tmp -Directory | Select-Object -First 1
    if (-not $src) { Fail "The downloaded archive is empty." }
    New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
    # Copy over an existing installation (updates the program, keeps .venv).
    Copy-Item -Path (Join-Path $src.FullName "*") -Destination $AppDir -Recurse -Force
    Remove-Item $zip, $tmp -Recurse -Force -ErrorAction SilentlyContinue
    Ok "Installed to $AppDir"
}

# --------------------------------------------------------------------------
# Virtual environment and dependencies
# --------------------------------------------------------------------------
Step "Installing the required libraries (first time: 1-3 minutes)"
$venvPy  = Join-Path $AppDir ".venv\Scripts\python.exe"
$venvPyw = Join-Path $AppDir ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $venvPy)) {
    & $python -m venv (Join-Path $AppDir ".venv")
    if ($LASTEXITCODE -ne 0) { Fail "Could not create the Python environment." }
}
& $venvPy -m pip install --disable-pip-version-check --upgrade pip --quiet
& $venvPy -m pip install --disable-pip-version-check -r (Join-Path $AppDir "requirements.txt") --quiet
if ($LASTEXITCODE -ne 0) { Fail "Installing the libraries failed (see the messages above)." }
Ok "Libraries installed"

Step "Checking the installation"
Push-Location $AppDir
& $venvPy main.py --self-test
$selfTest = $LASTEXITCODE
Pop-Location
if ($selfTest -ne 0) { Fail "The self-test reported a problem (see above)." }

# --------------------------------------------------------------------------
# Shortcuts
# --------------------------------------------------------------------------
Step "Creating shortcuts"
$shell = New-Object -ComObject WScript.Shell
$targets = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "$AppName.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "$AppName.lnk")
)
foreach ($lnkPath in $targets) {
    $lnk = $shell.CreateShortcut($lnkPath)
    $lnk.TargetPath = $venvPyw
    $lnk.Arguments = "`"$(Join-Path $AppDir 'main.py')`""
    $lnk.WorkingDirectory = $AppDir
    $lnk.Description = "Attach application PDFs to Gmail drafts (never sends e-mails)"
    $lnk.IconLocation = "$venvPyw,0"
    $lnk.Save()
}
Ok "Desktop and Start menu shortcut: $AppName"

Write-Host ""
Write-Host "INSTALLATION COMPLETE" -ForegroundColor Green
Write-Host "Start the program with the '$AppName' icon on your Desktop."
Write-Host "Next step: connect Gmail - see README.md section 5 for creating your Google OAuth file."
$answer = Read-Host "Start the program now? (Y/n)"
if ($answer -eq "" -or $answer -match "^[YyJj]") {
    Start-Process -FilePath $venvPyw -ArgumentList "`"$(Join-Path $AppDir 'main.py')`"" -WorkingDirectory $AppDir
}
