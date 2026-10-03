<#
Start AeroChorus on this PC, e.g. after a restart (runbook section 1).

  powershell -ExecutionPolicy Bypass -File C:\Users\sagej\Projects\atctranscribe\deploy\windows\start-aerochorus.ps1

Works from any folder. In order:
  1. Docker Desktop (started if needed), then Postgres + the API (docker compose).
  2. The review UI on http://127.0.0.1:8080, in its own window (skipped if already running).
  3. A check that the worker can read the archive share; says how to reconnect it if not.
  4. The worker loop in this window (run-worker.ps1). Ctrl-C stops only the worker;
     the active model run is handed back and resumes on the next start.
Safe to run again: nothing that is already running is started twice.
#>
param(
  [string]$Config = "$env:APPDATA\aerochorus\worker.toml"
)
$ErrorActionPreference = "Continue"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$exe = Join-Path $repo ".venv\Scripts\aerochorus.exe"
if (-not (Test-Path $exe)) { throw "not found: $exe (run 'uv sync' in $repo first)" }
Set-Location $repo

function Step([string]$Text) { Write-Host "`n== $Text" -ForegroundColor Cyan }

$running = Get-CimInstance Win32_Process -Filter "Name = 'aerochorus.exe'" |
  Where-Object { $_.CommandLine -match ' worker run' }
if ($running) {
  Write-Host "A worker is already running (PID $($running[0].ProcessId)). Not starting a second one." -ForegroundColor Yellow
  exit 0
}

Step "Docker Desktop"
docker info *> $null
if ($LASTEXITCODE -ne 0) {
  $desktop = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
  if (Test-Path $desktop) { Start-Process $desktop }
  Write-Host "waiting for Docker Desktop to start..."
  $deadline = (Get-Date).AddMinutes(5)
  do {
    Start-Sleep -Seconds 5
    docker info *> $null
  } until ($LASTEXITCODE -eq 0 -or (Get-Date) -gt $deadline)
  if ($LASTEXITCODE -ne 0) { throw "Docker Desktop did not come up within 5 minutes; start it by hand and run this again" }
}
Write-Host "Docker is running"

Step "Database and API"
docker compose up -d
$deadline = (Get-Date).AddMinutes(3)
$health = $null
do {
  try { $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 5 } catch { Start-Sleep -Seconds 3 }
} until ($health -or (Get-Date) -gt $deadline)
if (-not $health) { throw "the API did not answer on http://127.0.0.1:8000/health; check 'docker compose logs api'" }
Write-Host "API: $($health.status)"

Step "Review UI"
$listening = Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue
if ($listening) {
  Write-Host "already running on http://127.0.0.1:8080"
} else {
  Start-Process powershell -WorkingDirectory $repo -ArgumentList @(
    "-NoExit", "-Command",
    "`$Host.UI.RawUI.WindowTitle = 'AeroChorus review UI'; & '$exe' ui serve --config '$Config'"
  )
  Write-Host "started in its own window: http://127.0.0.1:8080 (close that window to stop it)"
}

Step "Archive share"
$report = (& $exe worker health --config $Config 2>$null) -join "`n" | ConvertFrom-Json
$down = @($report.sources.PSObject.Properties | Where-Object { -not $_.Value.available })
foreach ($source in $down) {
  Write-Host ("{0} is not readable: {1}" -f $source.Name, $source.Value.root) -ForegroundColor Yellow
}
if ($down) {
  Write-Host "Open the share in File Explorer, sign in to the NAS and tick 'Remember my credentials'." -ForegroundColor Yellow
  Write-Host "The worker starts anyway and waits; its runs continue once the share is readable."
} else {
  Write-Host "all sources readable"
}

Step "Worker (Ctrl-C to stop; the run resumes on the next start)"
& (Join-Path $PSScriptRoot "run-worker.ps1") -Config $Config
