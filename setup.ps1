<#
.SYNOPSIS
  TimeScribe from-source setup. Clone the repo, run this, done.

.DESCRIPTION
  Default: installs Python deps, fetches ActivityWatch portable, creates
  Start Menu + optional startup shortcuts that run TimeScribe from source.
  No exe, no SmartScreen, no signing needed.

.PARAMETER BuildExe
  Also build the frozen TimeScribe.exe with PyInstaller (optional).

.PARAMETER SkipAW
  Skip the ~150 MB ActivityWatch download (use if AW is already installed).

.PARAMETER NoStartup
  Don't add the run-at-login shortcut.

.PARAMETER Uninstall
  Remove shortcuts, the pip package, and optionally all data.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File setup.ps1
#>
param(
    [switch]$BuildExe,
    [switch]$SkipAW,
    [switch]$NoStartup,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$Here = $PSScriptRoot
$StartMenu = [Environment]::GetFolderPath("Programs")
$Startup   = [Environment]::GetFolderPath("Startup")

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

function Get-Python {
    foreach ($cmd in @("py -3", "python")) {
        try {
            $v = Invoke-Expression "$cmd --version 2>&1"
            if ($v -match "Python 3\.(1[0-9]|[1-9][0-9])") { return $cmd }
        } catch { }
    }
    return $null
}

function New-Shortcut($LinkPath, $Target, $Arguments, $WorkDir, $Icon) {
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($LinkPath)
    $sc.TargetPath = $Target
    $sc.Arguments = $Arguments
    $sc.WorkingDirectory = $WorkDir
    if ($Icon) { $sc.IconLocation = $Icon }
    $sc.Save()
}

# ---------------- Uninstall ----------------
if ($Uninstall) {
    Write-Step "Removing shortcuts"
    Remove-Item "$StartMenu\TimeScribe.lnk" -ErrorAction SilentlyContinue
    Remove-Item "$Startup\TimeScribe.lnk" -ErrorAction SilentlyContinue

    Write-Step "Stopping TimeScribe"
    Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -like "*timescribe*app*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

    Write-Step "Uninstalling Python package"
    $py = Get-Python
    if ($py) { Invoke-Expression "$py -m pip uninstall timescribe -y" }

    $ans = Read-Host "Also delete all data (settings, digests, drafts, credentials)? [y/N]"
    if ($ans -eq "y") {
        Remove-Item "$env:LOCALAPPDATA\timescribe" -Recurse -Force -ErrorAction SilentlyContinue
        cmdkey /delete:timescribe 2>$null
        cmdkey /delete:timescribe.halo 2>$null
        Write-Host "Data and credentials removed."
    }
    Write-Host "`nUninstalled. This folder can now be deleted." -ForegroundColor Green
    exit 0
}

# ---------------- Install ----------------
Write-Step "Checking Python"
$py = Get-Python
if (-not $py) {
    Write-Host "Python 3.10+ not found. Installing via winget..." -ForegroundColor Yellow
    winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
    $py = Get-Python
    if (-not $py) { throw "Python install failed - install Python 3.12+ manually and re-run." }
}
Write-Host "Using: $py ($(Invoke-Expression "$py --version 2>&1"))"

Write-Step "Installing TimeScribe + dependencies"
Invoke-Expression "$py -m pip install -e `"$Here`" --quiet"

if (-not $SkipAW) {
    # Skip the download entirely if ActivityWatch is already present:
    # running server, an installed copy, or a previously fetched bundle.
    $awRunning = $false
    try {
        $r = Invoke-WebRequest "http://127.0.0.1:5600/api/0/info" -TimeoutSec 2 -UseBasicParsing
        $awRunning = ($r.StatusCode -eq 200)
    } catch { }
    $awInstalled = (Test-Path "$env:LOCALAPPDATA\Programs\ActivityWatch\aw-qt.exe") -or
                   (Test-Path "C:\Program Files\ActivityWatch\aw-qt.exe")

    if ($awRunning) {
        Write-Host "ActivityWatch is already running on this machine - skipping bundle download."
    } elseif ($awInstalled) {
        Write-Host "ActivityWatch is already installed - skipping bundle download."
    } elseif (Test-Path "$Here\aw_dist\activitywatch\aw-qt.exe") {
        Write-Host "ActivityWatch bundle already fetched."
    } else {
        Write-Step "Fetching ActivityWatch portable (~150 MB, one time)"
        Invoke-Expression "$py `"$Here\fetch_aw.py`""
    }
}

if ($BuildExe) {
    Write-Step "Building frozen TimeScribe.exe (optional extra)"
    Invoke-Expression "$py -m pip install pyinstaller --quiet"
    Invoke-Expression "$py -m PyInstaller `"$Here\pad.spec`" --noconfirm"
    Write-Host "Built: $Here\dist\TimeScribe\TimeScribe.exe"
}

Write-Step "Creating shortcuts"
# pythonw = no console window; run the tray app from source
$pyw = (Invoke-Expression "$py -c `"import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))`"").Trim()
$icon = "$Here\pad.ico"
New-Shortcut "$StartMenu\TimeScribe.lnk" $pyw "-m timescribe app" $Here $icon
Write-Host "Start Menu shortcut created."
if (-not $NoStartup) {
    New-Shortcut "$Startup\TimeScribe.lnk" $pyw "-m timescribe app" $Here $icon
    Write-Host "Run-at-login shortcut created (skip with -NoStartup)."
}

Write-Step "Launching TimeScribe"
$alreadyRunning = $false
try {
    $r = Invoke-WebRequest "http://127.0.0.1:8770/api/status" -TimeoutSec 2 -UseBasicParsing
    $alreadyRunning = ($r.StatusCode -eq 200)
} catch { }
if ($alreadyRunning) {
    Write-Host "TimeScribe is already running - not starting a second instance."
} else {
    Start-Process $pyw -ArgumentList "-m timescribe app" -WorkingDirectory $Here
}

Write-Host @"

Done. The dashboard should open shortly (tray icon near the clock).

Next steps in the Setup card:
  1. HaloPSA URL + OAuth Client ID  -> Save settings -> Connect to Halo
  2. Pick an AI provider and paste its API key (or choose 'MCP only')

Uninstall later with:  powershell -File setup.ps1 -Uninstall
"@ -ForegroundColor Green
