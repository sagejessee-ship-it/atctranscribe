#!/usr/bin/env bash
# One-time preparation of the Linux GPU host for the AeroChorus containers (ADR-023).
# Idempotent: every step checks first and only changes what is missing. No
# repository, Python, Node or editor is installed. The host gets the NVIDIA driver,
# Docker Engine with the Compose plugin, the NVIDIA Container Toolkit, and the
# read-only CIFS mount of the collector's archive.
#
#   sudo bash host-setup.sh [options]
#     --data DIR          data root (default /srv/aerochorus)
#     --share //HOST/SHARE  collector archive (default //192.168.68.84/bwi)
#     --mount DIR         where to mount it read-only (default /mnt/aerochorus/atc)
#     --install-driver    install the NVIDIA 580 driver if none/too old (then reboot)
#     --stop-frigate      stop and disable a running Frigate container (frees the GPU)
#     --skip-mount        do not touch the CIFS mount
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="/srv/aerochorus"
SHARE="//192.168.68.84/bwi"
MOUNT="/mnt/aerochorus/atc"
INSTALL_DRIVER=0
STOP_FRIGATE=0
SKIP_MOUNT=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --data) DATA="$2"; shift 2 ;;
    --share) SHARE="$2"; shift 2 ;;
    --mount) MOUNT="$2"; shift 2 ;;
    --install-driver) INSTALL_DRIVER=1; shift ;;
    --stop-frigate) STOP_FRIGATE=1; shift ;;
    --skip-mount) SKIP_MOUNT=1; shift ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
RUN_USER="${SUDO_USER:-root}"
MIN_DRIVER=570  # CUDA 12.8 runtime in the worker image; Pascal is supported through 580

say() { printf '\n== %s\n' "$*"; }
ok() { printf '   ok: %s\n' "$*"; }
warn() { printf '   WARNING: %s\n' "$*"; }
die() { printf '   ERROR: %s\n' "$*" >&2; exit 1; }
. /etc/os-release

say "1. inventory (read-only)"
mkdir -p "$DATA/logs"
bash "$HERE/inventory.sh" --json "$DATA/logs/inventory-$(date +%Y%m%d-%H%M%S).json" --mount "$MOUNT" || true

say "2. NVIDIA driver"
driver_major() { nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 | cut -d. -f1; }
if ! command -v nvidia-smi >/dev/null || [[ -z "$(driver_major)" ]] || (( $(driver_major) < MIN_DRIVER )); then
  if (( INSTALL_DRIVER )); then
    apt-get update
    apt-get install -y ubuntu-drivers-common
    ubuntu-drivers install nvidia:580 || apt-get install -y nvidia-driver-580
    echo "   driver installed: reboot, then run this script again"
    exit 0
  fi
  die "NVIDIA driver >= $MIN_DRIVER needed (found: $(driver_major || echo none)). Re-run with --install-driver (installs 580, the last branch for Pascal), then reboot."
fi
nvidia-smi --query-gpu=name,compute_cap,memory.total,driver_version --format=csv,noheader
ok "driver $(driver_major) (>= $MIN_DRIVER)"

say "3. Docker Engine + Compose plugin"
if ! command -v docker >/dev/null; then
  # Docker's official apt repository (https://docs.docker.com/engine/install/ubuntu/).
  apt-get update
  apt-get install -y ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$ID ${VERSION_CODENAME} stable" \
    >/etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
docker compose version >/dev/null 2>&1 || apt-get install -y docker-compose-plugin
systemctl enable --now docker >/dev/null
ok "$(docker --version); $(docker compose version | head -1)"
if [[ "$RUN_USER" != root ]] && ! id -nG "$RUN_USER" | grep -qw docker; then
  usermod -aG docker "$RUN_USER"
  warn "$RUN_USER added to the docker group: log out and back in before ./deploy.sh"
fi

say "4. NVIDIA Container Toolkit (GPU inside containers)"
if ! command -v nvidia-ctk >/dev/null; then
  # NVIDIA's apt repository (https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/).
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    >/etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt-get update
  apt-get install -y nvidia-container-toolkit
fi
if ! grep -q nvidia /etc/docker/daemon.json 2>/dev/null; then
  nvidia-ctk runtime configure --runtime=docker
  systemctl restart docker
fi
if docker run --rm --gpus all ubuntu:24.04 nvidia-smi -L; then
  ok "containers see the GPU"
else
  die "a container could not see the GPU (check the driver and nvidia-ctk)"
fi

say "5. GPU users (Frigate etc.)"
FRIGATE="$(docker ps --format '{{.Names}} {{.Image}}' | awk 'tolower($0) ~ /frigate/ {print $1}')"
if [[ -n "$FRIGATE" ]]; then
  if (( STOP_FRIGATE )); then
    for c in $FRIGATE; do docker update --restart=no "$c" >/dev/null; docker stop "$c" >/dev/null; done
    ok "stopped and disabled: $FRIGATE"
  else
    warn "Frigate is running ($FRIGATE) and may hold GPU memory and ports. Re-run with --stop-frigate, or stop it yourself."
  fi
fi
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader | sed 's/^/   GPU process: /' || true
for port in 8080 8000 5432; do
  if ss -ltn "sport = :$port" | grep -q LISTEN; then
    warn "port $port is already in use: $(ss -ltnp "sport = :$port" | tail -1 | awk '{print $NF}')"
  fi
done

say "6. collector archive: read-only CIFS mount"
if (( SKIP_MOUNT )); then
  echo "   skipped (--skip-mount)"
else
  command -v mount.cifs >/dev/null || apt-get install -y cifs-utils
  install -d -m 700 /etc/aerochorus
  if [[ ! -f /etc/aerochorus/smb.cred ]]; then
    echo "   credentials for $SHARE (stored in /etc/aerochorus/smb.cred, root-only):"
    read -rp "   share user: " SMB_USER
    read -rsp "   share password: " SMB_PASS; echo
    install -m 600 /dev/null /etc/aerochorus/smb.cred
    printf 'username=%s\npassword=%s\n' "$SMB_USER" "$SMB_PASS" >/etc/aerochorus/smb.cred
    unset SMB_PASS
  fi
  mkdir -p "$MOUNT"
  uid="$(id -u "$RUN_USER")"; gid="$(id -g "$RUN_USER")"
  line="$SHARE  $MOUNT  cifs  ro,credentials=/etc/aerochorus/smb.cred,uid=$uid,gid=$gid,file_mode=0444,dir_mode=0555,iocharset=utf8,vers=3.0,nofail,_netdev,x-systemd.automount,x-systemd.mount-timeout=30  0  0"
  if ! grep -qs " $MOUNT " /etc/fstab; then
    printf '# AeroChorus: collector archive, read-only (ADR-002)\n%s\n' "$line" >>/etc/fstab
    systemctl daemon-reload
  fi
  mountpoint -q "$MOUNT" || mount "$MOUNT"
  ls "$MOUNT" >/dev/null || die "cannot list $MOUNT"
  grep -qs " $MOUNT cifs ro[, ]" /proc/mounts || die "$MOUNT is not mounted read-only"
  if touch "$MOUNT/.aerochorus-write-probe" 2>/dev/null; then
    rm -f "$MOUNT/.aerochorus-write-probe"
    die "$MOUNT accepted a write: it must be read-only"
  fi
  ok "$SHARE on $MOUNT, read-only (a write was refused); top level: $(ls "$MOUNT" | head -5 | tr '\n' ' ')"
fi

say "7. data directories under $DATA"
for d in config deploy postgres models artifacts artifacts-jesseepc corpora exports backups logs cache; do
  mkdir -p "$DATA/$d"
done
chown "$RUN_USER": "$DATA" "$DATA"/{config,deploy,models,artifacts,artifacts-jesseepc,corpora,exports,backups,logs,cache}
chmod 700 "$DATA/config" "$DATA/backups"
ln -sf "$DATA/deploy/aerochorus" /usr/local/bin/aerochorus
df -h "$DATA" | tail -1 | awk '{print "   free on " $6 ": " $4}'

say "done"
cat <<EOF
   Next, as $RUN_USER (after logging in again if you were just added to the docker group):
     bash deploy.sh up        (from this bundle folder)
   Settings and secrets then live in $DATA/config/aerochorus.env (chmod 600).
EOF
