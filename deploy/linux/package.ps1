<#
Build the AeroChorus images on this PC and write a deploy bundle for the Linux GPU host
(ADR-023). The Linux host then needs no repository, Python, Node or editor: only the
NVIDIA driver, Docker with the NVIDIA Container Toolkit, and the read-only share mount
(host-setup.sh does those).

  powershell -File deploy\linux\package.ps1 [-Out <dir>] [-NoImages]

Result: <Out>\aerochorus-deploy-<tag>\
  images-<tag>.tar.gz   aerochorus-app, aerochorus-worker, postgres:17 (docker load)
  deploy.sh host-setup.sh aerochorus import.sh backup.sh inventory.sh compose.yml
  aerochorus.env.example worker.toml.example fstab.example counts.sql crispasr.lock
  README.md IMAGES.txt SHA256SUMS

Copy the folder to the Linux host (scp -r, a USB disk or a share), then there:
  sudo bash host-setup.sh   (once)
  bash deploy.sh up         (first install and every update)
-NoImages writes the kit only (for a script-only update; the images must already be loaded).
#>
param(
  [string]$Out = "$env:USERPROFILE\aerochorus-data\deploy",
  [switch]$NoImages
)
$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Push-Location $repo
try {
  $rev = (git rev-parse --short HEAD).Trim()
  $dirty = [bool](git status --porcelain --untracked-files=no)
  $tag = "{0}-{1}{2}" -f (Get-Date -Format "yyyy.MM.dd"), $rev, $(if ($dirty) { "-dirty" } else { "" })
  if ($dirty) { Write-Warning "uncommitted changes: the images carry the tag $tag" }
  $app = "aerochorus-app:$tag"
  $worker = "aerochorus-worker:$tag"

  if (-not $NoImages) {
    Write-Host "1/4 build $app and $worker"
    docker build --target app -t $app --build-arg "AEROCHORUS_REVISION=$rev" .
    if ($LASTEXITCODE) { throw "app image build failed" }
    docker build --target worker -t $worker --build-arg "AEROCHORUS_REVISION=$rev" .
    if ($LASTEXITCODE) { throw "worker image build failed" }
    docker pull postgres:17
    if ($LASTEXITCODE) { throw "docker pull postgres:17 failed" }
  }

  $dir = Join-Path $Out "aerochorus-deploy-$tag"
  Write-Host "2/4 kit -> $dir"
  New-Item -ItemType Directory -Force $dir | Out-Null
  $kit = "compose.yml", "deploy.sh", "aerochorus", "backup.sh", "import.sh", "host-setup.sh",
         "inventory.sh", "aerochorus.env.example", "worker.toml.example", "fstab.example",
         "crispasr.lock", "README.md"
  foreach ($f in $kit) { Copy-Item (Join-Path "deploy\linux" $f) $dir -Force }
  Copy-Item "deploy\migrate\counts.sql" $dir -Force

  $lines = @("TAG=$tag", "REVISION=$rev", "BUILT_UTC=$((Get-Date).ToUniversalTime().ToString('s'))Z")
  if (-not $NoImages) {
    Write-Host "3/4 save images (a few GB; takes several minutes)"
    $tar = Join-Path $dir "images-$tag.tar"
    docker save -o $tar $app $worker postgres:17
    if ($LASTEXITCODE) { throw "docker save failed" }
    python -c "import gzip, shutil, sys; src = sys.argv[1]; dst = gzip.open(src + '.gz', 'wb', compresslevel=3); shutil.copyfileobj(open(src, 'rb'), dst, 1 << 20); dst.close()" $tar
    if ($LASTEXITCODE) { throw "compressing the images failed" }
    Remove-Item $tar
    foreach ($image in $app, $worker, "postgres:17") {
      $id = (docker image inspect --format "{{.Id}}" $image).Trim()
      $lines += "IMAGE=$image $id"
    }
  }
  # LF line endings: these files are read on Linux.
  [IO.File]::WriteAllText((Join-Path $dir "IMAGES.txt"), ($lines -join "`n") + "`n")

  Write-Host "4/4 checksums"
  $sums = Get-ChildItem $dir -File | Where-Object Name -ne "SHA256SUMS" | Sort-Object Name | ForEach-Object {
    "{0}  {1}" -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower(), $_.Name
  }
  [IO.File]::WriteAllText((Join-Path $dir "SHA256SUMS"), ($sums -join "`n") + "`n")
  Get-ChildItem $dir | Format-Table Name, @{ n = "MB"; e = { [math]::Round($_.Length / 1MB, 1) } }
  Write-Host "Bundle ready: $dir"
  Write-Host "Copy it to the Linux host, e.g.:  scp -r `"$dir`" <user>@<linux-host>:~/"
  Write-Host "Then on the host:  cd ~/aerochorus-deploy-$tag; sudo bash host-setup.sh; bash deploy.sh up"
} finally {
  Pop-Location
}
