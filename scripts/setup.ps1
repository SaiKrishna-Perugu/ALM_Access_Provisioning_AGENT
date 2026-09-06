# ============================================================================
#  setup.ps1  -  one-time bootstrap for the ALM Access Retrieval + GPT agent
#
#  Creates an isolated .venv and installs all dependencies INTO IT, so the
#  agent never touches your global Python or other projects.
#
#  Usage (from anywhere):
#      .\scripts\setup.ps1
#
#  Online install is tried first. If PyPI is blocked (corporate network),
#  it falls back to an offline .\wheels folder. See README.md (Offline install).
# ============================================================================
[CmdletBinding()]
param(
    [string]$WheelsDir,
    # Also install pytest + ruff so the offline test suite can be run locally.
    [switch]$Dev
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

# 2) Create the virtual environment if it does not exist.
$venvPy = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "Creating virtual environment in .venv ..."
    & $py -m venv (Join-Path $repoRoot ".venv")
}
if (-not (Test-Path $venvPy)) { throw "Failed to create .venv" }
Write-Host "Venv Python : $venvPy"

# 3) Install dependencies into the venv: online first, offline wheels as fallback.
$req = Join-Path $repoRoot "requirements.txt"
Write-Host "`nInstalling dependencies (online) ..."
& $venvPy -m pip install --timeout 15 --retries 1 -r $req
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Online install failed - PyPI may be blocked on this network."
    if (Test-Path $WheelsDir) {
        Write-Host "Falling back to offline wheels: $WheelsDir"
        & $venvPy -m pip install --no-index --find-links $WheelsDir -r $req
        if ($LASTEXITCODE -ne 0) { throw "Offline install from '$WheelsDir' failed." }
    }
    else {
        throw "PyPI is unreachable and no wheels folder was found at '$WheelsDir'. See README.md (Offline install)."
    }
}

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

Write-Host "`nSetup complete. Next steps:" -ForegroundColor Green
Write-Host "  1) Edit .env  (EWM_SERVER, CID, and ALM_CA_BUNDLE to verify TLS)."
Write-Host "  2) Retrieve : .\.venv\Scripts\python.exe src\alm_access_requests.py --user <CID>"
Write-Host "  3) Provision: .\scripts\start-gpt.ps1   (log into GPT)"
Write-Host "                .\.venv\Scripts\python.exe src\elm_gpt.py          (dry run)"
Write-Host "                .\.venv\Scripts\python.exe src\elm_gpt.py --commit (add users)"
