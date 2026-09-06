# Starts Chrome with remote debugging on 9222 using a dedicated profile in Incognito mode.
# GPT must be accessed from a private window; the Windows Kerberos ticket still
# passes through, so GPT authenticates.
param([string]$Url = "https://gpt.fiatspa.com/GlobalProvisioningTool/home.jsf")
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
$repoRoot = Split-Path $PSScriptRoot -Parent
$profileDir = Join-Path $repoRoot "chrome-debug"
# A fresh user-data-dir carries no SSO policy, so Negotiate/Kerberos must be allowed
# explicitly for the GPT host, else the navigation fails with ERR_INVALID_AUTH_CREDENTIALS.
$authHost = ([Uri]$Url).Host
$flags = @(
    "--incognito"
    "--remote-debugging-port=9222"
    "--user-data-dir=`"$profileDir`""
    "--auth-server-allowlist=`"*$authHost`""
    "--auth-negotiate-delegate-allowlist=`"*$authHost`""
    "--no-first-run"
    "--no-default-browser-check"
) -join " "
try { $up = (Invoke-WebRequest http://127.0.0.1:9222/json/version -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 } catch { $up = $false }
if (-not $up) {
    Start-Process $chrome "$flags $Url"
    Start-Sleep -Seconds 3
}
Write-Host "Debug Chrome ready on 9222. Log in to GPT, then run: .\.venv\Scripts\python.exe src\elm_gpt.py --commit"
