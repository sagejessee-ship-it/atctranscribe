<#
Start AeroChorus on this PC: after a restart, or whenever something is not running
(runbook "Start here"). From the project folder:

  .\start-aerochorus.ps1          start whatever is not running
  .\start-aerochorus.ps1 -Check   only report what is running; start nothing

Works from any folder with the full path:
  powershell -ExecutionPolicy Bypass -File C:\Users\sagej\Projects\atctranscribe\deploy\windows\start-aerochorus.ps1

In order, each step skipped if it is already up:
  1. Docker Desktop, then Postgres + the API (docker compose).
  2. The review UI on http://127.0.0.1:8080, in its own window (restarts itself if it exits).
  3. A check that the archive share is readable.
  4. The worker loop, in this window. If a worker is already running, it is left alone:
     it picks up newly queued runs by itself.
#>
param(
  [string]$Config = "$env:APPDATA\aerochorus\worker.toml",
  [switch]$Check
)
$ErrorActionPreference = "Continue"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$exe = Join-Path $repo ".venv\Scripts\aerochorus.exe"
if (-not (Test-Path $exe)) { throw "not found: $exe (run 'uv sync' in $repo first)" }
Set-Location $repo

function Step([string]$Text) { Write-Host "`n== $Text" -ForegroundColor Cyan }
function Ok([string]$Text) { Write-Host "   $Text" -ForegroundColor Green }
function Warn([string]$Text) { Write-Host "   $Text" -ForegroundColor Yellow }
function Info([string]$Text) { Write-Host "   $Text" }
$problems = 0

Step "Docker Desktop"
docker info *> $null
if ($LASTEXITCODE -ne 0) {
  if ($Check) {
    Warn "not running"
    $problems++
  } else {
    $desktop = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
    if (Test-Path $desktop) { Start-Process $desktop }
    Info "starting Docker Desktop and waiting for it..."
    $deadline = (Get-Date).AddMinutes(5)
    do {
      Start-Sleep -Seconds 5
      docker info *> $null
    } until ($LASTEXITCODE -eq 0 -or (Get-Date) -gt $deadline)
    if ($LASTEXITCODE -ne 0) { throw "Docker Desktop did not come up within 5 minutes; start it by hand and run this again" }
    Ok "running"
  }
} else {
  Ok "running"
}

Step "Database and API"
if (-not $Check) { docker compose up -d 2>&1 | Out-Null }
$health = $null
$deadline = (Get-Date).AddSeconds($(if ($Check) { 5 } else { 180 }))
do {
  try { $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 5 } catch { Start-Sleep -Seconds 3 }
} until ($health -or (Get-Date) -gt $deadline)
if ($health) {
  Ok "API $($health.status) on http://127.0.0.1:8000"
} else {
  Warn "the API does not answer on http://127.0.0.1:8000/health (see: docker compose logs api)"
  $problems++
}

Step "Review UI"
if (Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue) {
  Ok "running: http://127.0.0.1:8080"
} elseif ($Check) {
  Warn "not running (run this script without -Check to start it)"
  $problems++
} else {
  Start-Process powershell -WorkingDirectory $repo -ArgumentList @(
    "-NoExit", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $PSScriptRoot "run-ui.ps1"), "-Config", $Config
  )
  $deadline = (Get-Date).AddSeconds(30)
  do { Start-Sleep -Seconds 1 } until ((Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue) -or (Get-Date) -gt $deadline)
  if (Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue) {
    Ok "started in its own window: http://127.0.0.1:8080 (leave that window open)"
  } else {
    Warn "started, but not answering yet; check its window or logs\ui-<date>.log"
    $problems++
  }
}

Step "Archive share"
$report = (& $exe worker health --config $Config 2>$null) -join "`n" | ConvertFrom-Json
$down = @($report.sources.PSObject.Properties | Where-Object { -not $_.Value.available })
if ($down) {
  foreach ($source in $down) { Warn ("{0} is not readable: {1}" -f $source.Name, $source.Value.root) }
  Info "Open the share in File Explorer, sign in to the NAS and tick 'Remember my credentials'."
  Info "The worker waits meanwhile; its runs continue once the share is readable."
} else {
  Ok "all sources readable"
}

Step "Worker"
$workers = @(Get-CimInstance Win32_Process -Filter "Name = 'aerochorus.exe'" |
  Where-Object { $_.CommandLine -match ' worker (run|transcribe)' })
$transcribe = "http://127.0.0.1:8080/transcribe"
if ($workers) {
  $w = $workers[0]
  Ok ("already running (PID {0}, since {1:g}): it picks up newly queued runs by itself" -f $w.ProcessId, $w.CreationDate)
  if ($w.CommandLine -notmatch '--config') {
    Info "(started by hand, so no restart loop or log file; to change that: Ctrl-C it in its window, then run this script again)"
  }
  if ($problems) {
    Write-Host "`nNot ready: see the yellow lines above. Run .\start-aerochorus.ps1 (without -Check) to start what is missing." -ForegroundColor Yellow
  } else {
    Write-Host "`nReady. Queue a run on $transcribe" -ForegroundColor Green
  }
  exit $problems
}
if ($Check) {
  Warn "not running (run this script without -Check to start it)"
  exit ($problems + 1)
}
Write-Host "`nReady. Queue a run on $transcribe" -ForegroundColor Green
Write-Host "This window is now the worker. Leave it open; Ctrl-C stops it (the active run resumes on the next start)." -ForegroundColor Cyan
$Host.UI.RawUI.WindowTitle = "AeroChorus worker (leave open)"
& (Join-Path $PSScriptRoot "run-worker.ps1") -Config $Config
