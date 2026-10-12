<#
Serve the AeroChorus review UI (http://127.0.0.1:8080) and restart it if it ever exits.
Started by start-aerochorus.ps1 in its own window; logs to
%USERPROFILE%\aerochorus-data\logs\ui-<date>.log. Close the window to stop it.
#>
param(
  [string]$Config = "$env:APPDATA\aerochorus\worker.toml",
  [int]$RestartDelaySeconds = 10
)
$ErrorActionPreference = "Continue"
$Host.UI.RawUI.WindowTitle = "AeroChorus review UI (leave open)"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$exe = Join-Path $repo ".venv\Scripts\aerochorus.exe"
Set-Location $repo
$logDir = Join-Path $env:USERPROFILE "aerochorus-data\logs"
New-Item -ItemType Directory -Force $logDir | Out-Null

function Write-Log([string]$Line, [string]$Path) {
  Write-Host $Line
  Add-Content -Path $Path -Value $Line -Encoding UTF8
}

while ($true) {
  $log = Join-Path $logDir ("ui-{0}.log" -f (Get-Date -Format "yyyyMMdd"))
  Write-Log "==== $((Get-Date).ToString('s')) starting the review UI on http://127.0.0.1:8080" $log
  & $exe ui serve --config $Config 2>&1 | ForEach-Object { Write-Log "$_" $log }
  Write-Log ("==== $((Get-Date).ToString('s')) review UI exited (code $LASTEXITCODE); " +
    "restarting in $RestartDelaySeconds s (close this window to stop it)") $log
  Start-Sleep -Seconds $RestartDelaySeconds
}
