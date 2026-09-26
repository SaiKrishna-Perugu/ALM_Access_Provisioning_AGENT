# ============================================================================
#  setup.ps1  -  one-time bootstrap for the ALM Access Retrieval + GPT agent
#
#  Creates an isolated .venv and installs all dependencies INTO IT, so the
#  agent never touches your global Python or other projects.
#
#  Usage (from anywhere):
#      .\scripts\setup.ps1            # the CLI
#      .\scripts\setup.ps1 -Agents    # the CLI + the multi-agent system (agent_local.py)
#
#  Online install is tried first. If PyPI is blocked (corporate network),
#  it falls back to an offline .\wheels folder. See README.md (Offline install).
# ============================================================================
[CmdletBinding()]
param(
    [string]$WheelsDir,
    # Also install pytest + ruff so the offline test suite can be run locally.
    [switch]$Dev,
    # Also install the multi-agent system (requirements-cloud.txt): LangGraph,
    # Gemini clients, the SQLite ledger, Playwright.
    [switch]$Agents
)
$ErrorActionPreference = "Stop"
$repoRoot = Split-Path $PSScriptRoot -Parent
if (-not $WheelsDir) { $WheelsDir = Join-Path $repoRoot "wheels" }
Set-Location $repoRoot

# 1) Find a base Python interpreter.
$py = $null
foreach ($cmd in @("python", "py")) {
    $c = Get-Command $cmd -ErrorAction SilentlyContinue
    if ($c) { $py = $c.Source; break }
}
if (-not $py) {
    throw "Python not found. Install Python 3.10+ (https://www.python.org/downloads/) and re-run."
}
Write-Host "Base Python : $py"

# 2) Create the virtual environment if it does not exist - or if it is broken.
#    A venv keeps a launcher that points at the Python it was made with; after
#    that Python is upgraded or removed, python.exe still exists but cannot run.
$venvPy = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPy) {
    $healthy = $false
    try {
        & $venvPy -c "import sys" 2>$null
        $healthy = ($LASTEXITCODE -eq 0)
    } catch { $healthy = $false }
    if (-not $healthy) {
        Write-Warning ".venv is broken (its Python no longer runs) - recreating it."
        Remove-Item -Recurse -Force (Join-Path $repoRoot ".venv")
    }
}
if (-not (Test-Path $venvPy)) {
    Write-Host "Creating virtual environment in .venv ..."
    & $py -m venv (Join-Path $repoRoot ".venv")
}
if (-not (Test-Path $venvPy)) { throw "Failed to create .venv" }
Write-Host "Venv Python : $venvPy"

# 3) Install dependencies into the venv: online first, offline wheels as fallback.
#    requirements-cloud.txt includes requirements.txt, so -Agents installs both.
function Invoke-Pip([string[]]$pipArgs) {
    # pip writes notices and warnings to stderr. Under "Stop", Windows
    # PowerShell 5.1 turns any stderr line into a terminating error when output
    # is redirected, so judge pip by its exit code alone.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    # Out-Host: pip's output goes to the console, not into this function's
    # return value, so the caller receives the exit code alone.
    try { & $venvPy -m pip @pipArgs --disable-pip-version-check 2>&1 | Out-Host } finally { $ErrorActionPreference = $previous }
    return $LASTEXITCODE
}

function Install-Requirements([string]$file) {
    Write-Host "`nInstalling $(Split-Path $file -Leaf) (online) ..."
    if ((Invoke-Pip @("install", "--timeout", "15", "--retries", "1", "-r", $file)) -eq 0) { return }
    Write-Warning "Online install failed - PyPI may be blocked on this network."
    if (Test-Path $WheelsDir) {
        Write-Host "Falling back to offline wheels: $WheelsDir"
        if ((Invoke-Pip @("install", "--no-index", "--find-links", $WheelsDir, "-r", $file)) -ne 0) {
            throw "Offline install of '$file' from '$WheelsDir' failed."
        }
    }
    else {
        throw "PyPI is unreachable and no wheels folder was found at '$WheelsDir'. See README.md (Offline install)."
    }
}
$reqName = if ($Agents) { "requirements-cloud.txt" } else { "requirements.txt" }
Install-Requirements (Join-Path $repoRoot $reqName)

# 3b) Optional: development tooling (offline test suite + linter).
if ($Dev) {
    $devReq = Join-Path $repoRoot "requirements-dev.txt"
    Write-Host "`nInstalling development dependencies ..."
    & $venvPy -m pip install --timeout 15 --retries 1 -r $devReq
    if ($LASTEXITCODE -ne 0 -and (Test-Path $WheelsDir)) {
        & $venvPy -m pip install --no-index --find-links $WheelsDir -r $devReq
    }
    if ($LASTEXITCODE -ne 0) { Write-Warning "Dev dependencies not installed - 'pytest' will be unavailable." }
}

# 4) Make sure a .env exists (copied from the template on first run).
$envFile = Join-Path $repoRoot ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $repoRoot ".env.example") $envFile
    Write-Host "`nCreated .env from .env.example - edit it to set EWM_SERVER and CID."
}

# 5) Verify the install.
& $venvPy -c "import dotenv, playwright; print('Dependencies OK')"
if ($Agents) {
    & $venvPy -c "import langgraph, langchain_google_genai, aiosqlite, pydantic_settings; print('Agent dependencies OK')"
    if ($LASTEXITCODE -ne 0) { throw "The agent dependencies did not import - see the pip output above." }
}

if ($Agents) {
    Write-Host "`nSetup complete (agents). Next steps:" -ForegroundColor Green
    Write-Host "  1) Edit .env  (EWM_SERVER, JTS_SERVER, CID, GEMINI_API_KEY; ALM_CA_BUNDLE to verify TLS)."
    Write-Host "  2) .\scripts\start-gpt.ps1   (sign in to GPT; only for runs that add AD groups)"
    Write-Host "  3) .\.venv\Scripts\python.exe src\agent_local.py --check"
    Write-Host "  4) .\.venv\Scripts\python.exe src\agent_local.py --work-item <id>   (dry run)"
    return
}
Write-Host "`nSetup complete. Next steps:" -ForegroundColor Green
Write-Host "  1) Edit .env  (EWM_SERVER, CID, and ALM_CA_BUNDLE to verify TLS)."
Write-Host "  2) Retrieve : .\.venv\Scripts\python.exe src\alm_access_requests.py --user <CID>"
Write-Host "  3) Provision: .\scripts\start-gpt.ps1   (log into GPT)"
Write-Host "                .\.venv\Scripts\python.exe src\elm_gpt.py          (dry run)"
Write-Host "                .\.venv\Scripts\python.exe src\elm_gpt.py --commit (add users)"
