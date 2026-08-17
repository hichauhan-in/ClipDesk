# End-to-end smoke test against a running ClipDesk server.
# Uploads the scratch video + transcript, runs the analysis, then exercises the
# clip and clean-cut renderers. Not part of the unit suite - it needs ffmpeg,
# a running server and a reachable model.
param(
    [string]$BaseUrl  = 'http://127.0.0.1:8799',
    [string]$Video    = '.scratch/meeting.mp4',
    [string]$Srt      = '.scratch/meeting.srt',
    [string]$Provider = 'copilot_cli'
)

$ErrorActionPreference = 'Stop'
Set-Location -Path (Split-Path -Parent (Split-Path -Parent $PSCommandPath))

function Wait-Job($jobId, $label, $timeoutSeconds = 900) {
    $deadline = (Get-Date).AddSeconds($timeoutSeconds)
    $lastMessage = ''
    while ((Get-Date) -lt $deadline) {
        $job = (Invoke-WebRequest "$BaseUrl/api/jobs/$jobId" -UseBasicParsing).Content | ConvertFrom-Json
        $latest = ($job.events | Where-Object { $_.message } | Select-Object -Last 1).message
        if ($latest -and $latest -ne $lastMessage) {
            Write-Host "    $latest" -ForegroundColor DarkGray
            $lastMessage = $latest
        }
        if ($job.status -eq 'done') { return $job }
        if ($job.status -eq 'failed') { throw "$label failed: $($job.error)" }
        Start-Sleep -Milliseconds 1500
    }
    throw "$label timed out"
}

Write-Host "==> Uploading" -ForegroundColor Cyan
$form = @{
    video      = Get-Item $Video
    transcript = Get-Item $Srt
    title      = 'Checkout retry policy review'
}
$project = (Invoke-WebRequest "$BaseUrl/api/projects" -Method Post -Form $form -UseBasicParsing).Content | ConvertFrom-Json
Write-Host "    project = $($project.id)"

Write-Host "==> Analysing (provider: $Provider)" -ForegroundColor Cyan
$body = @{ llm_provider = $Provider; skip_llm = $false } | ConvertTo-Json
$started = (Invoke-WebRequest "$BaseUrl/api/projects/$($project.id)/analyze" -Method Post -Body $body -ContentType 'application/json' -UseBasicParsing).Content | ConvertFrom-Json
Wait-Job $started.job_id 'Analysis' | Out-Null

$analysis = (Invoke-WebRequest "$BaseUrl/api/projects/$($project.id)/analysis" -UseBasicParsing).Content | ConvertFrom-Json
Write-Host "`n--- ANALYSIS ---" -ForegroundColor Green
Write-Host "title    : $($analysis.title)"
Write-Host "abstract : $($analysis.abstract)"
Write-Host "summary  : $($analysis.summary)"
Write-Host "keywords : $($analysis.keywords -join ', ')"
Write-Host "chapters : $($analysis.chapters.Count)"
$analysis.chapters | ForEach-Object { Write-Host ("  {0,7:N0}s  {1}" -f $_.start, $_.title) }
Write-Host "clips    : $($analysis.clip_candidates.Count)"
$analysis.clip_candidates | ForEach-Object { Write-Host ("  {0,7:N0}s  [{1:N2}] {2}" -f $_.start, $_.score, $_.title) }
Write-Host "decisions:"; $analysis.decisions | ForEach-Object { Write-Host "  - $($_.text)" }
Write-Host "actions  :"; $analysis.action_items | ForEach-Object { Write-Host "  - $($_.text) ($($_.owner))" }
Write-Host "dropped  : $(($analysis.segment_analyses | Where-Object { -not $_.keep }).Count) of $($analysis.segment_analyses.Count) segments"
$analysis.segment_analyses | Where-Object { -not $_.keep } | ForEach-Object {
    Write-Host ("  {0,6:N0}s  {1,-10} {2}" -f $_.start, $_.kind, $_.reason) -ForegroundColor DarkGray
}
if ($analysis.warnings) { Write-Host "warnings : $($analysis.warnings -join ' | ')" -ForegroundColor Yellow }

Write-Host "`n==> Clean-cut plan" -ForegroundColor Cyan
$plan = (Invoke-WebRequest "$BaseUrl/api/projects/$($project.id)/cleanup/plan" -Method Post -Body '{}' -ContentType 'application/json' -UseBasicParsing).Content | ConvertFrom-Json
Write-Host "    $($plan.summary) across $($plan.span_count) spans ($($plan.removed_percent)% removed)"

Write-Host "`n==> Rendering clean cut" -ForegroundColor Cyan
$started = (Invoke-WebRequest "$BaseUrl/api/projects/$($project.id)/cleanup" -Method Post -Body '{}' -ContentType 'application/json' -UseBasicParsing).Content | ConvertFrom-Json
Wait-Job $started.job_id 'Cleanup' | Out-Null

Write-Host "`n==> Cutting a 60s clip about the idempotency key" -ForegroundColor Cyan
$body = @{ target_seconds = 60; query = 'the idempotency key and why retrying payments is safe'; count = 1; use_llm = $true } | ConvertTo-Json
$started = (Invoke-WebRequest "$BaseUrl/api/projects/$($project.id)/clip" -Method Post -Body $body -ContentType 'application/json' -UseBasicParsing).Content | ConvertFrom-Json
$job = Wait-Job $started.job_id 'Clip'
$job.result.clips | ForEach-Object { Write-Host ("    {0:N0}s-{1:N0}s  {2}" -f $_.start, $_.end, $_.title) }

Write-Host "`n==> Writing notes" -ForegroundColor Cyan
$started = (Invoke-WebRequest "$BaseUrl/api/projects/$($project.id)/notes" -Method Post -Body '{}' -ContentType 'application/json' -UseBasicParsing).Content | ConvertFrom-Json
Wait-Job $started.job_id 'Notes' | Out-Null

Write-Host "`n--- OUTPUTS ---" -ForegroundColor Green
$final = (Invoke-WebRequest "$BaseUrl/api/projects/$($project.id)" -UseBasicParsing).Content | ConvertFrom-Json
$final.artifacts | ForEach-Object {
    Write-Host ("  {0,-46} {1,9:N0} KB  {2}" -f $_.filename, ($_.size_bytes / 1KB), $_.label)
}
Write-Host "`nProject id: $($final.id)" -ForegroundColor Green
