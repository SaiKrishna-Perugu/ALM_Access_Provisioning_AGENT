# Starts Chrome with remote debugging using a dedicated profile in Incognito mode.
# GPT must be accessed from a private window; the Windows Kerberos ticket still
# passes through, so GPT authenticates.
# The debugging port comes from CDP_URL (environment, then .env; default 9222),
# the same setting the CLI and the agents use to attach to this window.
param(
    [string]$Url = "",
    [switch]$Fresh
)

$repoRoot = Split-Path $PSScriptRoot -Parent
$envFile = Join-Path $repoRoot ".env"

if (-not $Url) {
    if ($env:GPT_URL) {
        $Url = $env:GPT_URL
    } elseif (Test-Path $envFile) {
        $line = Get-Content $envFile | Where-Object { $_ -match '^\s*GPT_URL\s*=' } | Select-Object -Last 1
        if ($line) { $Url = ($line -split '=', 2)[1].Trim().Trim('"').Trim("'") }
    }
    if (-not $Url) {
        $Url = "https://gpt.example.intra/GlobalProvisioningTool/home.jsf"
    }
}

# Locate chrome.exe (path differs across machines); fall back to PATH.
$chromeCandidates = @(
    (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"),
    (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe")
)
$chrome = $chromeCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chrome) { $chrome = (Get-Command chrome -ErrorAction SilentlyContinue).Source }
if (-not $chrome) {
    Write-Error "Google Chrome not found. Install Chrome or set the path in scripts/start-gpt.ps1."
    exit 1
}

# F2: Browser profiles moved to LOCALAPPDATA outside the repository to prevent syncing session cookies
$profileDir = $env:ALM_CHROME_USER_DATA_DIR
if (-not $profileDir) {
    $profileDir = Join-Path (Join-Path $env:LOCALAPPDATA "alm-agent") "chrome-debug"
}

if ($Fresh -and (Test-Path $profileDir)) {
    Write-Host "Removing existing profile directory: $profileDir"
    Remove-Item -Recurse -Force $profileDir -ErrorAction SilentlyContinue
}
if (-not (Test-Path $profileDir)) {
    New-Item -ItemType Directory -Path $profileDir -Force | Out-Null
}

$cdpUrl = $env:CDP_URL
if (-not $cdpUrl -and (Test-Path $envFile)) {
    $line = Get-Content $envFile | Where-Object { $_ -match '^\s*CDP_URL\s*=' } | Select-Object -Last 1
    if ($line) { $cdpUrl = ($line -split '=', 2)[1].Trim().Trim('"').Trim("'") }
}
if (-not $cdpUrl) { $cdpUrl = "http://127.0.0.1:9222" }
try { $cdp = [Uri]$cdpUrl } catch { Write-Error "CDP_URL '$cdpUrl' is not a URL (expected e.g. http://127.0.0.1:9222)."; exit 1 }
$port = if ($cdp.IsDefaultPort) { 9222 } else { $cdp.Port }

# A fresh user-data-dir carries no SSO policy, so Negotiate/Kerberos must be allowed
# explicitly for the GPT host, else the navigation fails with ERR_INVALID_AUTH_CREDENTIALS.
$authHost = ([Uri]$Url).Host
$flags = @(
    "--incognito"
    "--remote-debugging-port=$port"
    "--user-data-dir=`"$profileDir`""
    "--auth-server-allowlist=`"*$authHost`""
    "--auth-negotiate-delegate-allowlist=`"*$authHost`""
    "--no-first-run"
    "--no-default-browser-check"
) -join " "
try { $up = (Invoke-WebRequest "http://$($cdp.Host):$port/json/version" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 } catch { $up = $false }
if (-not $up) {
    Start-Process $chrome "$flags $Url"
    Start-Sleep -Seconds 3
}
Write-Host "Debug Chrome ready on port $port with profile at $profileDir."
Write-Host "Sign in to GPT in that window."
Write-Host "It stays open for whichever you run next - both attach to it:"
Write-Host "  the CLI:    .\.venv\Scripts\python.exe src\elm_gpt.py            (dry run; add --commit to write)"
Write-Host "  the agents: .\.venv\Scripts\python.exe src\agent_local.py --check"
