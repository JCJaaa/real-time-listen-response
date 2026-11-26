param(
    [switch]$RegisterGlobalSkill
)

$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoDir

Write-Host "=== Teams Live Copilot - Windows Setup ===" -ForegroundColor Cyan

# 1. Locate Python 3.10+
$PyCmd = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    try {
        $ver = & py -3 --version 2>&1
        if ($LASTEXITCODE -eq 0) { $PyCmd = @("py", "-3") }
    } catch {}
}
if (-not $PyCmd -and (Get-Command python -ErrorAction SilentlyContinue)) {
    try {
        $ver = & python --version 2>&1
        if ($LASTEXITCODE -eq 0) { $PyCmd = @("python") }
    } catch {}
}

if (-not $PyCmd) {
    Write-Host "Python 3 not found. Attempting automatic installation via winget..." -ForegroundColor Yellow
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id Python.Python.3.11 -e --source winget --accept-package-agreements --accept-source-agreements
        # Refresh PATH in current session so python/py is immediately usable without reopening terminal
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
        if (Get-Command py -ErrorAction SilentlyContinue) {
            $PyCmd = @("py", "-3")
        } elseif (Get-Command python -ErrorAction SilentlyContinue) {
            $PyCmd = @("python")
        } else {
            $LocalPy = Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"
            if (Test-Path $LocalPy) {
                $PyCmd = @($LocalPy)
            } else {
                Write-Host "Python installed, please double-click start.bat again to finish setup." -ForegroundColor Yellow
                exit 1
            }
        }
    } else {
        Write-Host "Please install Python 3.11+ from https://www.python.org/downloads/ (check 'Add Python to PATH') and double-click start.bat again." -ForegroundColor Red
        exit 1
    }
}

Write-Host "Using Python: $($PyCmd -join ' ')" -ForegroundColor Green

# 2. Create virtual environment (.venv)
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating virtual environment (.venv)..." -ForegroundColor Cyan
    if ($PyCmd.Count -eq 2) {
        & $PyCmd[0] $PyCmd[1] -m venv .venv
    } else {
        & $PyCmd[0] -m venv .venv
    }
}

$VenvPy = Join-Path $RepoDir ".venv\Scripts\python.exe"

# 3. Install dependencies
Write-Host "Installing Python dependencies..." -ForegroundColor Cyan
& $VenvPy -m pip install --upgrade pip
& $VenvPy -m pip install -r requirements.txt

# 4. Verify audio loopback device discovery
Write-Host "`nChecking Windows WASAPI loopback devices..." -ForegroundColor Cyan
& $VenvPy teams_live_copilot.py --list-devices

# 5. Optionally register as a global Antigravity skill
if ($RegisterGlobalSkill) {
    $SkillTarget = Join-Path $env:USERPROFILE ".gemini\antigravity\skills\teams-live-copilot"
    New-Item -ItemType Directory -Force -Path $SkillTarget | Out-Null
    Copy-Item -Path (Join-Path $RepoDir "*") -Destination $SkillTarget -Recurse -Force -Exclude @(".venv", ".git", ".env", "__pycache__")
    Write-Host "`nRegistered Antigravity skill at: $SkillTarget" -ForegroundColor Green
}

Write-Host "`nSetup complete! Run .\start.bat or .\start.ps1 to launch." -ForegroundColor Green
