# Purges local runtime data and CLI artifacts older than a given number of days.
# Default retention period: 30 days.
param(
    [int]$Days = 30,
    [switch]$DryRun
)

$repoRoot = Split-Path $PSScriptRoot -Parent
$outDir = Join-Path $repoRoot "out"

if (-not (Test-Path $outDir)) {
    Write-Host "No out/ directory found. Nothing to purge."
    exit 0
}

$cutoff = (Get-Date).AddDays(-$Days)
Write-Host "Purging local data older than $Days days (before $($cutoff.ToString('yyyy-MM-dd HH:mm:ss')))..."

$targets = @(
    @{ Path = (Join-Path $outDir "audit"); Pattern = "*.json"; Name = "CLI audit records" },
    @{ Path = (Join-Path $outDir "screenshots"); Pattern = "*.png"; Name = "CLI screenshots" },
    @{ Path = $outDir; Pattern = "alm_users*.json"; Name = "User caches" },
    @{ Path = $outDir; Pattern = "comment_capture.json"; Name = "Comment capture cache" }
)

$totalDeleted = 0

foreach ($t in $targets) {
    if (Test-Path $t.Path) {
        $files = Get-ChildItem -Path $t.Path -Filter $t.Pattern -File -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -lt $cutoff }
        foreach ($file in $files) {
            if ($DryRun) {
                Write-Host "  [DRY RUN] Would delete: $($file.FullName)"
            } else {
                Remove-Item -Force $file.FullName -ErrorAction SilentlyContinue
                Write-Host "  Deleted: $($file.Name)"
            }
            $totalDeleted++
        }
    }
}

# Run agent purge if python environment exists
$py = Join-Path (Join-Path $repoRoot ".venv") "Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
$agentLocal = Join-Path (Join-Path $repoRoot "src") "agent_local.py"

if (Test-Path $agentLocal) {
    Write-Host "Running agent local purge for checkpoints, memories, and reports..."
    & $py $agentLocal --purge-older-than $Days
}

Write-Host "Local purge complete. Total CLI files removed: $totalDeleted"
