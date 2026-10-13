# Run daily: fetch threads, describe new photos, rebuild the topic graph,
# then commit and push so GitHub Pages + Muse (reading needs-reply.md /
# needs-like.md) stay current.
# Register this script with Task Scheduler (see README.md).

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

& "$PSScriptRoot\.venv\Scripts\python.exe" -m src.main

git add data/needs-reply.md data/needs-like.md data/actions.md data/topics.md data/threads data/history docs/graph.json
$changes = git status --porcelain data/needs-reply.md data/needs-like.md data/actions.md data/topics.md data/threads data/history docs/graph.json
if ($changes) {
    git commit -m "Update thread topic graph ($(Get-Date -Format 'yyyy-MM-dd HH:mm'))"
    git push
} else {
    Write-Host "No changes to commit."
}
