# Ausbildung Job Finder - one-click installer for Windows
#
# Installs Python 3.12 (only if needed), downloads the program (no Git
# needed), installs the browser-control library (Playwright) and creates
# shortcuts.  It uses your installed Microsoft Edge (or Google Chrome); only
# if neither exists is a browser downloaded.
#
# Run again at any time to update; collected jobs and settings are kept.
#
# Usage: double-click Install_JobFinder.bat, or paste into a Command Prompt:
#   powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/Aminehaouam/gmailpy/claude/great-brown-eyx5q1/jobfinder/install_jobfinder.ps1 | iex"

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$AppName      = "Ausbildung Job Finder"
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
# Program files
# --------------------------------------------------------------------------
Step "Getting the program files"
$here = $PSScriptRoot
if ($here -and (Test-Path (Join-Path $here "jobfinder_main.py"))) {
    $JobDir = $here
    Ok "Using the files in $JobDir"
} else {
    $zip = Join-Path $env:TEMP "bewerbung-jobfinder.zip"
    $tmp = Join-Path $env:TEMP "bewerbung-jobfinder-extract"
    try { Invoke-WebRequest -Uri $RepoZipUrl -OutFile $zip -UseBasicParsing }
    catch { Fail "Could not download the program ($($_.Exception.Message))." }
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    $src = Get-ChildItem $tmp -Directory | Select-Object -First 1
    if (-not $src) { Fail "The downloaded archive is empty." }
    New-Item -ItemType Directory -Force -Path $DefaultDir | Out-Null
    Copy-Item -Path (Join-Path $src.FullName "*") -Destination $DefaultDir -Recurse -Force
    Remove-Item $zip, $tmp -Recurse -Force -ErrorAction SilentlyContinue
    $JobDir = Join-Path $DefaultDir "jobfinder"
    Ok "Installed to $JobDir"
}

# --------------------------------------------------------------------------
# Environment and libraries
# --------------------------------------------------------------------------
Step "Installing the browser-control library (first time: 1-2 minutes)"
$venv    = Join-Path $JobDir ".venv"
$venvPy  = Join-Path $venv "Scripts\python.exe"
$venvPyw = Join-Path $venv "Scripts\pythonw.exe"
if (-not (Test-Path $venvPy)) {
    & $python -m venv $venv
    if ($LASTEXITCODE -ne 0) { Fail "Could not create the Python environment." }
}
& $venvPy -m pip install --disable-pip-version-check --upgrade pip --quiet
& $venvPy -m pip install --disable-pip-version-check -r (Join-Path $JobDir "requirements.txt") --quiet
if ($LASTEXITCODE -ne 0) { Fail "Installing the libraries failed (see the messages above)." }
Ok "Libraries installed"

Step "Checking for a browser"
$browsers = @(
    (Join-Path ${env:ProgramFiles(x86)} "Microsoft\Edge\Application\msedge.exe"),
    (Join-Path $env:ProgramFiles "Microsoft\Edge\Application\msedge.exe"),
    (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"),
    (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe")
)
$found = $browsers | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if ($found) {
    Ok "Using $found"
} else {
    Info "Neither Edge nor Chrome found - downloading a browser for the program ..."
    & $venvPy -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { Fail "Could not download a browser." }
    Ok "Browser installed"
}

Step "Checking the installation"
& $venvPy (Join-Path $JobDir "jobfinder_main.py") --self-test
if ($LASTEXITCODE -ne 0) { Fail "The self-test reported a problem (see above)." }

Step "Creating shortcuts"
$shell = New-Object -ComObject WScript.Shell
foreach ($folder in @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"))) {
    $lnk = $shell.CreateShortcut((Join-Path $folder "$AppName.lnk"))
    $lnk.TargetPath = $venvPyw
    $lnk.Arguments = "`"$(Join-Path $JobDir 'jobfinder_main.py')`""
    $lnk.WorkingDirectory = $JobDir
    $lnk.Description = "Ausbildungsstellen Elektroniker/in aus der Jobsuche der BA sammeln"
    $lnk.IconLocation = "$venvPyw,0"
    $lnk.Save()
}
Ok "Desktop and Start menu shortcut: $AppName"

Write-Host ""
Write-Host "INSTALLATION COMPLETE" -ForegroundColor Green
$answer = Read-Host "Start the program now? (Y/n)"
if ($answer -eq "" -or $answer -match "^[YyJj]") {
    Start-Process -FilePath $venvPyw -ArgumentList "`"$(Join-Path $JobDir 'jobfinder_main.py')`"" -WorkingDirectory $JobDir
}
