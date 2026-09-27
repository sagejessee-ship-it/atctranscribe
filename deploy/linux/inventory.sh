#!/usr/bin/env bash
# AeroChorus host inventory (ADR-021). Read-only: prints and records what this
# machine is, before anything is installed. Safe to re-run.
#
#   deploy/linux/inventory.sh [--json OUT] [--mount /mnt/aerochorus/atc]
set -uo pipefail

OUT=""
MOUNT="${AEROCHORUS_CORPUS_MOUNT:-/mnt/aerochorus/atc}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --json) OUT="$2"; shift 2 ;;
    --mount) MOUNT="$2"; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done

have() { command -v "$1" >/dev/null 2>&1; }
kv() { printf '  %-22s %s\n' "$1" "$2"; }

. /etc/os-release 2>/dev/null || true
OS="${PRETTY_NAME:-$(uname -s)}"
KERNEL="$(uname -r)"
CPU="$(awk -F: '/model name/ {gsub(/^ +/, "", $2); print $2; exit}' /proc/cpuinfo)"
CORES="$(nproc 2>/dev/null || echo '?')"
RAM_MB="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)"

GPU_CSV=""; CUDA_DRIVER=""; GPU_NAME=""; GPU_CC=""; GPU_MEM=""; DRIVER=""
if have nvidia-smi; then
  GPU_CSV="$(nvidia-smi --query-gpu=index,name,compute_cap,memory.total,driver_version --format=csv,noheader,nounits 2>/dev/null | head -1)"
  IFS=',' read -r _ GPU_NAME GPU_CC GPU_MEM DRIVER <<<"$GPU_CSV"
  GPU_NAME="$(echo "$GPU_NAME" | xargs)"; GPU_CC="$(echo "$GPU_CC" | xargs)"
  GPU_MEM="$(echo "$GPU_MEM" | xargs)"; DRIVER="$(echo "$DRIVER" | xargs)"
  CUDA_DRIVER="$(nvidia-smi | grep -oE 'CUDA (UMD )?Version: *[0-9.]+' | grep -oE '[0-9.]+$' | head -1)"
fi

CUDART12="no"
if ldconfig -p 2>/dev/null | grep -q 'libcudart.so.12'; then CUDART12="system"; fi
if ls /srv/aerochorus/cuda12/nvidia/cuda_runtime/lib/libcudart.so.12* >/dev/null 2>&1; then CUDART12="/srv/aerochorus/cuda12"; fi

MOUNT_STATE="absent"; MOUNT_OPTS=""; MOUNT_SOURCE=""
if have findmnt && findmnt -rn "$MOUNT" >/dev/null 2>&1; then
  MOUNT_SOURCE="$(findmnt -rno SOURCE "$MOUNT")"
  MOUNT_OPTS="$(findmnt -rno OPTIONS "$MOUNT")"
  MOUNT_STATE="mounted"
  [[ ",$MOUNT_OPTS," == *",ro,"* ]] && MOUNT_STATE="mounted-ro" || MOUNT_STATE="mounted-rw"
fi

PROFILE="unknown"
if [[ -n "$GPU_CC" ]]; then
  major="${GPU_CC%%.*}"
  case "$major" in
    6) arch=pascal ;; 7) [[ "$GPU_CC" == 7.5 ]] && arch=turing || arch=volta ;;
    8) [[ "$GPU_CC" == 8.9 ]] && arch=ada || arch=ampere ;; 9) arch=hopper ;;
    10|12) arch=blackwell ;; *) arch=legacy ;;
  esac
  PROFILE="linux_${arch}_$(( (GPU_MEM + 512) / 1024 ))gb"
else
  PROFILE="linux_cpu_$(( (RAM_MB + 512) / 1024 ))gb"
fi

echo "AeroChorus host inventory ($(date -Is))"
kv "hostname" "$(hostname)"
kv "os" "$OS"
kv "kernel" "$KERNEL"
kv "cpu" "$CPU ($CORES threads)"
kv "ram" "${RAM_MB} MB"
kv "gpu" "${GPU_NAME:-none} (cc ${GPU_CC:-?}, ${GPU_MEM:-?} MB)"
kv "nvidia driver" "${DRIVER:-none} (max CUDA ${CUDA_DRIVER:-?})"
kv "libcudart.so.12" "$CUDART12"
kv "docker" "$(docker --version 2>/dev/null || echo none)"
kv "corpus mount" "$MOUNT: $MOUNT_STATE ${MOUNT_SOURCE:+($MOUNT_SOURCE)}"
kv "profile" "$PROFILE"
echo "  storage:"
df -h --output=target,size,avail / /srv 2>/dev/null | sed 's/^/    /'

if [[ -n "$GPU_CC" ]]; then
  if awk "BEGIN{exit !($GPU_CC < 7.5)}"; then
    echo "  => compute capability $GPU_CC: use the CUDA 12 CrispASR build (CUDA 13 dropped Pascal)."
  fi
fi

if [[ -n "$OUT" ]]; then
  mkdir -p "$(dirname "$OUT")"
  cat >"$OUT" <<JSON
{
  "collected_at": "$(date -Is)",
  "hostname": "$(hostname)",
  "os": "$OS",
  "kernel": "$KERNEL",
  "cpu": "$CPU",
  "cpu_threads": $CORES,
  "ram_mb": $RAM_MB,
  "gpu": {"name": "$GPU_NAME", "compute_capability": "$GPU_CC", "memory_mb": "${GPU_MEM}", "driver": "$DRIVER", "driver_max_cuda": "$CUDA_DRIVER"},
  "libcudart12": "$CUDART12",
  "corpus_mount": {"path": "$MOUNT", "state": "$MOUNT_STATE", "source": "$MOUNT_SOURCE", "options": "$MOUNT_OPTS"},
  "suggested_profile": "$PROFILE"
}
JSON
  echo "  written: $OUT"
fi
