# Purges local run data older than a given number of days (default 30).
#
# One implementation, in Python (src/alm_agents/local.py: purge), so the rules
# are tested in one place. It removes the agents' checkpoints, approval cards,
# memory, reports and evidence, and the CLI's screenshots, user caches and
# comment capture. It keeps both ledgers and audit records: the CLI's out/audit
# and the agents' alm_idempotency / alm_audit are the record of what was
# written and approved.
#
#   .\scripts\purge-local.ps1                 # delete data older than 30 days
#   .\scripts\purge-local.ps1 -Days 7 -DryRun # show what would go; delete nothing
param(
    [ValidateRange(1, 3650)][int]$Days = 30,
    [switch]$DryRun
)

$repoRoot = Split-Path $PSScriptRoot -Parent
$py = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Error "No .venv found. Run .\scripts\setup.ps1 -Agents first."
    exit 1
}

$purgeArgs = @((Join-Path $repoRoot "src\agent_local.py"), "--purge-older-than", "$Days")
if ($DryRun) { $purgeArgs += "--dry-run" }

& $py @purgeArgs
exit $LASTEXITCODE
