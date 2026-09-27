# Export AeroChorus state from the Windows dev box (RTX 5080) for the Linux host.
# Read-only on the source: pg_dump + copies of AeroChorus-owned files. Collector
# audio is never copied. Models are re-downloaded on Linux (pinned by sha256).
#
#   powershell -File deploy\migrate\export-windows.ps1 [-Out C:\Users\sagej\aerochorus-data\migration]
param(
  [string]$Out = "$env:USERPROFILE\aerochorus-data\migration",
  [string]$Data = "$env:USERPROFILE\aerochorus-data"
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$dest = Join-Path $Out $stamp
New-Item -ItemType Directory -Force $dest | Out-Null
Push-Location $repo
try {
  Write-Host "1/4 pg_dump (custom format)"
  docker compose exec -T postgres pg_dump -U aerochorus -d aerochorus -Fc -f /tmp/aerochorus.dump
  docker compose cp postgres:/tmp/aerochorus.dump "$dest\aerochorus.dump"
  docker compose exec -T postgres rm -f /tmp/aerochorus.dump

  Write-Host "2/4 row counts + fingerprints"
  Get-Content deploy\migrate\counts.sql | docker compose exec -T postgres psql -U aerochorus -d aerochorus -At -F "`t" -f - |
    Set-Content -Encoding utf8 "$dest\counts.tsv"
  docker compose exec -T postgres psql -U aerochorus -d aerochorus -Atc "select version_num from alembic_version" |
    Set-Content -Encoding utf8 "$dest\alembic_version.txt"
} finally { Pop-Location }

Write-Host "3/4 AeroChorus-owned files (raw artifacts, ATCO2 benchmark clips)"
tar -C $Data -czf "$dest\artifacts-jesseepc.tgz" artifacts
if (Test-Path "$Data\corpora\atco2_fixed") { tar -C "$Data\corpora" -czf "$dest\atco2_fixed.tgz" atco2_fixed }

Write-Host "4/4 checksums"
Get-ChildItem $dest -File | Where-Object Name -ne "SHA256SUMS" | ForEach-Object {
  "{0}  {1}" -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower(), $_.Name
} | Set-Content -Encoding ascii "$dest\SHA256SUMS"
Get-ChildItem $dest | Format-Table Name, Length
Write-Host "Copy $dest to the Linux host, e.g.:"
Write-Host "  scp -r `"$dest`" <user>@<linux-host>:/srv/aerochorus/backups/migration-$stamp"
