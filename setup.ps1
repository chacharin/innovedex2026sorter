# ============================================================
#  Innovedex 2026 Sorter - One-shot setup & launch (uv edition)
#
#  Hardened version: handles common Windows quirks gracefully.
#   - PowerShell 5.1 / old TLS defaults
#   - Antivirus / corporate proxy / firewall
#   - PATH not refreshing / uv at multiple locations
#   - uv install failures -> 3-tier fallback (winget -> astral -> GitHub zip)
#   - Stale .venv / broken lock -> -Reinstall
#   - Failed imports / missing files -> caught before launch
#
#  Usage (PowerShell in the project folder):
#     powershell -ExecutionPolicy Bypass -File .\setup.ps1
#
#  Flags:
#     -SkipLaunch       Install only, do not launch
#     -Reinstall        Remove .venv and resync
#     -SkipPreflight    Skip environment checks (debug only)
#     -PythonVersion    Override Python version (default: 3.14)
#
#  Note: this script is intentionally all-ASCII so it parses
#  correctly under Windows PowerShell 5.1 (which reads .ps1
#  files in the legacy ANSI codepage by default).
# ============================================================

[CmdletBinding()]
param(
    [switch]$SkipLaunch,
    [switch]$Reinstall,
    [switch]$SkipPreflight,
    [string]$PythonVersion = "3.14"
)

$ErrorActionPreference = 'Stop'
# Silence the slow PS 5.1 progress bar for Invoke-WebRequest (10-100x speedup)
$ProgressPreference    = 'SilentlyContinue'

# --- Project root + logging ---------------------------------
$ProjectRoot = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
Set-Location -LiteralPath $ProjectRoot

$LogFile = Join-Path $ProjectRoot 'setup.log'
"==== Setup started at $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ====" |
    Out-File -FilePath $LogFile -Encoding utf8

function Log-Line($msg) { $msg | Out-File -FilePath $LogFile -Append -Encoding utf8 }

# --- Output helpers ----------------------------------------
function Write-Step($n, $total, $msg) {
    Write-Host ""
    Write-Host "==> [$n/$total] $msg" -ForegroundColor Cyan
    Log-Line "[$n/$total] $msg"
}
function Write-Ok($msg)    { Write-Host "  [OK]   $msg" -ForegroundColor Green;    Log-Line "  OK: $msg" }
function Write-Warn2($msg) { Write-Host "  [WARN] $msg" -ForegroundColor Yellow;   Log-Line "  WARN: $msg" }
function Write-Err2($msg)  { Write-Host "  [ERR]  $msg" -ForegroundColor Red;      Log-Line "  ERR: $msg" }
function Write-Hint($msg)  { Write-Host "         $msg" -ForegroundColor DarkGray; Log-Line "  HINT: $msg" }

# Force TLS 1.2 (PS 5.1 defaults to TLS 1.0; fails on most modern HTTPS)
try {
    [Net.ServicePointManager]::SecurityProtocol =
        [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch {}

# --- Helpers -----------------------------------------------
function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable('Path','Machine')
    $user    = [Environment]::GetEnvironmentVariable('Path','User')
    $extra   = @(
        (Join-Path $env:USERPROFILE '.local\bin'),
        (Join-Path $env:USERPROFILE '.cargo\bin')
    ) -join ';'
    $env:Path = "$machine;$user;$extra"
}

function Get-CmdPath($name) {
    try { (Get-Command $name -ErrorAction Stop).Source } catch { return $null }
}

function Test-Url($url, $timeoutSec = 5) {
    try {
        $req = [System.Net.WebRequest]::Create($url)
        $req.Method  = 'HEAD'
        $req.Timeout = $timeoutSec * 1000
        $resp = $req.GetResponse()
        $resp.Close()
        return $true
    } catch { return $false }
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Innovedex 2026 Sorter  -  Auto Setup (uv, hardened)" -ForegroundColor Cyan
Write-Host "  Project root: $ProjectRoot" -ForegroundColor Cyan
Write-Host "  Log file:     $LogFile" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

$TotalSteps = 6
$StartTime  = Get-Date

# ============================================================
# Step 1: Preflight checks
# ============================================================
Write-Step 1 $TotalSteps "Preflight"

if ($SkipPreflight) {
    Write-Warn2 "Preflight skipped (-SkipPreflight)."
} else {
    # PowerShell version
    $psVer = $PSVersionTable.PSVersion
    if ($psVer.Major -lt 5) {
        Write-Err2 "PowerShell $psVer is too old. Need 5.1 or later."
        Write-Hint "Install WMF 5.1: https://aka.ms/wmf5download"
        exit 1
    }
    Write-Ok "PowerShell $psVer"

    # Admin warning (uv works best as regular user)
    $isAdmin = ([Security.Principal.WindowsPrincipal] `
        [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($isAdmin) {
        Write-Warn2 "Running as Administrator (not required)."
        Write-Hint "uv/python may install to wrong scope. If you hit permission errors,"
        Write-Hint "re-run from a regular (non-elevated) PowerShell window."
    } else {
        Write-Ok "Running as regular user"
    }

    Write-Ok "ExecutionPolicy (effective): $(Get-ExecutionPolicy)"

    # Project path with spaces - usually fine but worth noting
    if ($ProjectRoot -match '\s') {
        Write-Warn2 "Project path contains spaces. Usually OK, but if you hit"
        Write-Hint "weird errors, move the project to a space-free path."
    }

    # Project directory writable
    $testFile = Join-Path $ProjectRoot ".setup-write-test"
    try {
        New-Item -ItemType File -Path $testFile -Force | Out-Null
        Remove-Item -LiteralPath $testFile -Force
        Write-Ok "Project directory writable"
    } catch {
        Write-Err2 "Cannot write to $ProjectRoot"
        Write-Hint "Move project to a writable location (e.g., Desktop / Documents)."
        exit 1
    }

    # Required project files present
    $required = @('pyproject.toml','uv.lock','launch_all.py','camera_node.py')
    $missing  = $required | Where-Object { -not (Test-Path (Join-Path $ProjectRoot $_)) }
    if ($missing) {
        Write-Err2 ("Missing project files: " + ($missing -join ', '))
        Write-Hint "Run this script from a folder containing pyproject.toml + launch_all.py."
        exit 1
    }
    Write-Ok "Project files present"

    # Disk space - torch + opencv + ultralytics is ~3-4 GB
    try {
        $drive  = (Get-Item -LiteralPath $ProjectRoot).PSDrive
        $freeGB = [math]::Round($drive.Free / 1GB, 1)
        if ($freeGB -lt 5) {
            Write-Warn2 "Drive $($drive.Name): has only $freeGB GB free. Recommend 5+ GB."
        } else {
            Write-Ok "Disk space: $freeGB GB free on $($drive.Name):"
        }
    } catch {}

    # Network reachability - fail fast if a proxy is in the way
    Write-Host "  ...testing network..." -ForegroundColor DarkGray
    $hosts = @(
        @{ Name='astral.sh';   Url='https://astral.sh' },
        @{ Name='pypi.org';    Url='https://pypi.org/simple/' },
        @{ Name='github.com';  Url='https://github.com' }
    )
    foreach ($h in $hosts) {
        if (Test-Url $h.Url) {
            Write-Ok "Reachable: $($h.Name)"
        } else {
            Write-Warn2 "Unreachable: $($h.Name) (proxy / firewall?)"
        }
    }
    if (-not (Test-Url 'https://pypi.org/simple/')) {
        Write-Err2 "pypi.org unreachable - uv sync will fail."
        Write-Hint "Check internet / VPN / proxy, then re-run."
        exit 1
    }
}

# ============================================================
# Step 2: Install / locate uv
# ============================================================
Write-Step 2 $TotalSteps "Install / locate uv"

$uvPath = Get-CmdPath 'uv'

if (-not $uvPath) {
    # --- 2a) Try winget first (signed, AV-friendly) ---
    if (Get-CmdPath 'winget') {
        Write-Host "  Trying winget..." -ForegroundColor DarkGray
        try {
            winget install --id=astral-sh.uv -e --source winget `
                --accept-source-agreements --accept-package-agreements --silent
            Update-SessionPath
            $uvPath = Get-CmdPath 'uv'
            if ($uvPath) { Write-Ok "uv installed via winget" }
        } catch {
            Write-Warn2 "winget install failed: $($_.Exception.Message)"
        }
    } else {
        Write-Warn2 "winget not available on this machine."
    }

    # --- 2b) Try Astral installer (irm | iex) ---
    if (-not $uvPath) {
        Write-Host "  Trying Astral installer..." -ForegroundColor DarkGray
        try {
            $installerScript = Invoke-RestMethod -Uri 'https://astral.sh/uv/install.ps1' -UseBasicParsing
            Invoke-Expression $installerScript
            Update-SessionPath
            $uvPath = Get-CmdPath 'uv'
            if (-not $uvPath) {
                $candidate = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
                if (Test-Path $candidate) { $uvPath = $candidate }
            }
            if ($uvPath) { Write-Ok "uv installed via Astral installer" }
        } catch {
            Write-Warn2 "Astral installer failed: $($_.Exception.Message)"
            Write-Hint "(Some AV/EDR products block 'irm | iex' patterns.)"
        }
    }

    # --- 2c) Last resort: direct GitHub release download ---
    if (-not $uvPath) {
        Write-Host "  Falling back to direct GitHub download..." -ForegroundColor DarkGray
        $arch = if ([Environment]::Is64BitOperatingSystem) { 'x86_64' } else { 'i686' }
        $zipUrl  = "https://github.com/astral-sh/uv/releases/latest/download/uv-$arch-pc-windows-msvc.zip"
        $zipPath = Join-Path $env:TEMP 'uv-download.zip'
        $destDir = Join-Path $env:USERPROFILE '.local\bin'
        try {
            Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing
            if (-not (Test-Path $destDir)) {
                New-Item -ItemType Directory -Path $destDir -Force | Out-Null
            }
            Expand-Archive -LiteralPath $zipPath -DestinationPath $destDir -Force
            Remove-Item -LiteralPath $zipPath -Force
            # Add to user PATH permanently (no admin needed)
            $userPath = [Environment]::GetEnvironmentVariable('Path','User')
            if ($userPath -notlike "*$destDir*") {
                [Environment]::SetEnvironmentVariable('Path', "$userPath;$destDir", 'User')
            }
            Update-SessionPath
            $candidate = Join-Path $destDir 'uv.exe'
            if (Test-Path $candidate) {
                $uvPath = $candidate
                Write-Ok "uv installed via GitHub release zip"
            }
        } catch {
            Write-Err2 "GitHub fallback failed: $($_.Exception.Message)"
        }
    }

    if (-not $uvPath -or -not (Test-Path $uvPath)) {
        Write-Err2 "All 3 uv install methods failed."
        Write-Hint "Manual install:"
        Write-Hint "  1) Download uv-x86_64-pc-windows-msvc.zip from"
        Write-Hint "     https://github.com/astral-sh/uv/releases/latest"
        Write-Hint "  2) Extract uv.exe to a folder on your PATH"
        Write-Hint "  3) Re-run this script"
        exit 1
    }
} else {
    Write-Ok "uv already installed: $uvPath"
}

# Use full path to uv - avoids PATH refresh issues in PS 5.1
$Uv = $uvPath
$uvVer = (& $Uv --version) -join ' '
Write-Ok "uv version: $uvVer"
Log-Line "uv path: $Uv"

# ============================================================
# Step 3: Ensure Python (managed by uv)
# ============================================================
Write-Step 3 $TotalSteps "Install Python $PythonVersion (managed by uv)"

# Retry once if first attempt fails (transient network)
$ok = $false
for ($attempt = 1; $attempt -le 2 -and -not $ok; $attempt++) {
    & $Uv python install $PythonVersion
    if ($LASTEXITCODE -eq 0) {
        $ok = $true
    } else {
        Write-Warn2 "uv python install attempt $attempt failed - retrying..."
        Start-Sleep -Seconds 3
    }
}
if (-not $ok) {
    Write-Err2 "Failed to install Python $PythonVersion via uv."
    Write-Hint "Try manually: & '$Uv' python install $PythonVersion --verbose"
    exit 1
}
Write-Ok "Python $PythonVersion ready (uv-managed, no system install)"

# ============================================================
# Step 4: uv sync (create .venv + install deps)
# ============================================================
Write-Step 4 $TotalSteps "uv sync (create .venv and install dependencies)"

if ($Reinstall) {
    foreach ($d in @('.venv','venv')) {
        $p = Join-Path $ProjectRoot $d
        if (Test-Path $p) {
            Write-Warn2 "Reinstall: removing $d ..."
            try {
                Remove-Item -LiteralPath $p -Recurse -Force
            } catch {
                Write-Warn2 "Could not remove $d : $($_.Exception.Message)"
                Write-Hint "Close any Python/Explorer windows holding files in $d, then retry."
            }
        }
    }
}

# Generous timeout for slow networks + disable ultralytics online checks
$env:UV_HTTP_TIMEOUT = '300'
$env:YOLO_OFFLINE    = 'True'

& $Uv sync
if ($LASTEXITCODE -ne 0) {
    Write-Err2 "uv sync failed (exit $LASTEXITCODE)"
    Write-Hint "Try: powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Reinstall"
    Write-Hint "Or check log: $LogFile"
    exit 1
}
Write-Ok "Dependencies installed in .venv"

# ============================================================
# Step 5: Verify install
# ============================================================
Write-Step 5 $TotalSteps "Verify install"

$venvPy = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Write-Err2 ".venv\Scripts\python.exe not found after uv sync"
    Write-Hint "Try: -Reinstall"
    exit 1
}
$pyVer = (& $venvPy --version 2>&1) -join ' '
Write-Ok "venv Python: $pyVer"

# Import sanity check - catches partial installs before launch
$importTest = @'
import sys
mods = ["cv2", "PIL", "zmq", "ultralytics", "customtkinter", "pyfirmata2"]
failed = []
for m in mods:
    try:
        __import__(m)
        print(f"  ok: {m}")
    except Exception as e:
        failed.append(m)
        print(f"  FAIL: {m} -> {e}")
if failed:
    print("FAIL: " + ",".join(failed))
    sys.exit(1)
'@
$importTest | & $venvPy -
if ($LASTEXITCODE -ne 0) {
    Write-Warn2 "Some packages failed to import (see above). Try -Reinstall."
} else {
    Write-Ok "All core imports working"
}

# YOLO model file
$modelPath = Join-Path $ProjectRoot '4color-detection.pt'
if (-not (Test-Path $modelPath)) {
    Write-Warn2 "4color-detection.pt not found."
    Write-Hint "Place the model file at: $modelPath"
    Write-Hint "Without it, camera_node will crash on startup."
} else {
    $sizeMB = [math]::Round((Get-Item -LiteralPath $modelPath).Length / 1MB, 1)
    Write-Ok "YOLO model found ($sizeMB MB)"
}

# data/ folder
$dataDir = Join-Path $ProjectRoot 'data'
if (-not (Test-Path $dataDir)) {
    New-Item -ItemType Directory -Path $dataDir | Out-Null
    Write-Ok "Created data/ directory (for CSV recordings)"
}

# ============================================================
# Step 6: Summary + Launch
# ============================================================
$elapsed = [math]::Round(((Get-Date) - $StartTime).TotalSeconds, 0)
Write-Step 6 $TotalSteps "Done (total: ${elapsed}s)"

Write-Host ""
Write-Host "  Installed:" -ForegroundColor White
Write-Host "    uv      : $Uv"
Write-Host "    python  : $venvPy"
Write-Host "    venv    : $(Join-Path $ProjectRoot '.venv')"
Write-Host ""
Write-Host "  Log file: $LogFile" -ForegroundColor DarkGray
Write-Host ""

if ($SkipLaunch) {
    Write-Host "  To launch later, run from this folder:" -ForegroundColor White
    Write-Host "      uv run python launch_all.py" -ForegroundColor White
    Write-Host ""
    exit 0
}

Write-Host "  Launching all nodes via launch_all.py ..." -ForegroundColor Green
Write-Host "  (If Windows Firewall prompts, click 'Allow access' for Private networks.)" -ForegroundColor DarkGray
Write-Host ""

& $Uv run python (Join-Path $ProjectRoot 'launch_all.py')
$rc = $LASTEXITCODE
Log-Line "launch_all.py exit code: $rc"
exit $rc
