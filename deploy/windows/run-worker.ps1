<#
Run the AeroChorus worker unattended on the Windows PC for days (runbook §14.8).

  powershell -ExecutionPolicy Bypass -File deploy\windows\run-worker.ps1

Runs `aerochorus worker run` (heartbeats + queued transcription runs) in a loop. If the
worker ever exits (crash, Docker hiccup), it starts again after 30 s. Output goes to the
console and to %USERPROFILE%\aerochorus-data\logs\worker-<date>.log. The worker keeps
Windows awake while a model run is active. Stop with Ctrl-C:
the active model run is handed back and resumes on the next start.
#>
param(
  [string]$Config = "$env:APPDATA\aerochorus\worker.toml",
  [int]$RestartDelaySeconds = 30
)
$ErrorActionPreference = "Continue"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$exe = Join-Path $repo ".venv\Scripts\aerochorus.exe"
if (-not (Test-Path $exe)) { throw "not found: $exe (run 'uv sync' in $repo first)" }
$logDir = Join-Path $env:USERPROFILE "aerochorus-data\logs"
New-Item -ItemType Directory -Force $logDir | Out-Null

# UTF-8 lines to the console and the log (Tee-Object would write UTF-16 in Windows PowerShell).
function Write-Log([string]$Line, [string]$Path) {
  Write-Host $Line
  Add-Content -Path $Path -Value $Line -Encoding UTF8
}

while ($true) {
  $log = Join-Path $logDir ("worker-{0}.log" -f (Get-Date -Format "yyyyMMdd"))
  $started = Get-Date
  Write-Log "==== $($started.ToString('s')) starting aerochorus worker run" $log
  & $exe worker run --config $Config 2>&1 | ForEach-Object { Write-Log "$_" $log }
  $code = $LASTEXITCODE
  Write-Log ("==== $((Get-Date).ToString('s')) worker exited (code $code) after " +
    "$([int]((Get-Date) - $started).TotalMinutes) min; restarting in $RestartDelaySeconds s (Ctrl-C to stop)") $log
  Start-Sleep -Seconds $RestartDelaySeconds
}
