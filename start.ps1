param(
    [string]$Context = "gbsh0010_context.md",
    [string]$Device = "",
    [string]$UI = "auto",
    [string]$Provider = ""
)

$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoDir

$VenvPy = Join-Path $RepoDir ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    Write-Host "First-time setup: installing environment & registering skill..." -ForegroundColor Yellow
    & powershell -ExecutionPolicy Bypass -File (Join-Path $RepoDir "install.ps1") -RegisterGlobalSkill
}

$EnvFile = Join-Path $RepoDir ".env"
$EnvMap = @{}
if (Test-Path $EnvFile) {
    Get-Content $EnvFile -Encoding utf8 | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $parts = $line.Split("=", 2)
            $EnvMap[$parts[0].Trim()] = $parts[1].Trim().Trim('"').Trim("'")
        }
    }
}

function Save-EnvMap {
    $lines = $EnvMap.GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Name)=$($_.Value)" }
    $lines | Out-File -FilePath $EnvFile -Encoding utf8
}

if (-not $Provider) {
    $DefaultProvider = if ($EnvMap.ContainsKey("AI_PROVIDER")) { $EnvMap["AI_PROVIDER"] } else { "gemini" }
    $gTag = if ($EnvMap.ContainsKey("GEMINI_API_KEY") -or $env:GEMINI_API_KEY) { " [key saved]" } else { "" }
    $oTag = if ($EnvMap.ContainsKey("OPENAI_API_KEY") -or $env:OPENAI_API_KEY) { " [key saved]" } else { "" }
    $cTag = if ($EnvMap.ContainsKey("ANTHROPIC_API_KEY") -or $env:ANTHROPIC_API_KEY) { " [key saved]" } else { "" }

    Write-Host "`nSelect AI Provider (default: $DefaultProvider):" -ForegroundColor Cyan
    Write-Host "  [1] Gemini (Recommended - Native Live Audio)$gTag"
    Write-Host "  [2] GPT (OpenAI)$oTag"
    Write-Host "  [3] Claude (Anthropic)$cTag"
    $Choice = (Read-Host "Enter 1, 2, or 3 (press Enter for $DefaultProvider)").Trim().ToLower()

    switch ($Choice) {
        ""       { $Provider = $DefaultProvider }
        "1"      { $Provider = "gemini" }
        "gemini" { $Provider = "gemini" }
        "2"      { $Provider = "gpt" }
        "gpt"    { $Provider = "gpt" }
        "3"      { $Provider = "claude" }
        "claude" { $Provider = "claude" }
        default  {
            Write-Host "Invalid choice '$Choice'. Using $DefaultProvider." -ForegroundColor Yellow
            $Provider = $DefaultProvider
        }
    }
}

$KeyVar = switch ($Provider) {
    "gemini" { "GEMINI_API_KEY" }
    "gpt"    { "OPENAI_API_KEY" }
    "claude" { "ANTHROPIC_API_KEY" }
    default  { throw "Unsupported provider: $Provider" }
}

$ExistingKey = [Environment]::GetEnvironmentVariable($KeyVar)
if (-not $ExistingKey -and $EnvMap.ContainsKey($KeyVar)) {
    $ExistingKey = $EnvMap[$KeyVar]
}

if (-not $ExistingKey) {
    $InputKey = (Read-Host "Enter your $KeyVar (saved locally to .env)").Trim()
    if (-not $InputKey) {
        Write-Host "$KeyVar is required." -ForegroundColor Red
        exit 1
    }
    $EnvMap[$KeyVar] = $InputKey
}

$EnvMap["AI_PROVIDER"] = $Provider
Save-EnvMap

$ArgsList = @("teams_live_copilot.py", "--provider", $Provider, "--ui", $UI)
if ($Context -and (Test-Path $Context)) {
    $ArgsList += @("--context", $Context)
}
if ($Device -ne "") {
    $ArgsList += @("--device", $Device)
}

Write-Host "Starting Teams Live Copilot ($Provider)..." -ForegroundColor Cyan
& $VenvPy @ArgsList
