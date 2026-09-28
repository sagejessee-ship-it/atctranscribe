#!/usr/bin/env bash
# AeroChorus on the Linux GTX 1070 host (ADR-021): install / verify, idempotently.
#
# Run from the repository checkout, as the user that will own the services
# (sudo is used for system files only):
#
#   deploy/linux/native/setup.sh                 # every phase, in order
#   deploy/linux/native/setup.sh --phase smoke   # one phase
#
# Phases: inventory mount driver python crispasr config services ui systemd smoke report
# Nothing here starts a production backlog: sweeps run only when someone creates one.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
HERE="$REPO/deploy/linux"
DATA="${AEROCHORUS_DATA:-/srv/aerochorus}"
MOUNT="${AEROCHORUS_CORPUS_MOUNT:-/mnt/aerochorus/atc}"
SOURCE_KEY="${AEROCHORUS_SOURCE_KEY:-home_atc_archive}"
ETC="/etc/aerochorus"
RUN_USER="${SUDO_USER:-$USER}"
LAN_IP="${AEROCHORUS_LAN_IP:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
PHASES=(inventory mount driver python crispasr config services ui systemd smoke report)
ONLY=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --phase) ONLY="$2"; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done

say() { printf '\n=== %s ===\n' "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }
uvx() { (cd "$REPO" && uv run "$@"); }

phase_inventory() {
  say "1-2. inventory (OS, CPU, RAM, storage, GPU, driver)"
  sudo mkdir -p "$DATA"/{postgres,models,artifacts,exports,cache,context,logs,backups}
  sudo chown -R "$RUN_USER" "$DATA"
  sudo chown -R 999:999 "$DATA/postgres" 2>/dev/null || true  # postgres container uid
  bash "$HERE/../inventory.sh" --json "$DATA/logs/inventory-$(date +%Y%m%d).json" --mount "$MOUNT"
}

phase_mount() {
  say "3-4. network corpus mount (read-only)"
  if ! findmnt -rn "$MOUNT" >/dev/null; then
    cat <<EOF
The corpus is not mounted at $MOUNT. Add a read-only CIFS mount (see
deploy/linux/fstab.example), then: sudo mkdir -p $MOUNT && sudo mount $MOUNT
EOF
    die "corpus mount missing"
  fi
  local opts; opts="$(findmnt -rno OPTIONS "$MOUNT")"
  if [[ ",$opts," == *",ro,"* ]]; then echo "mounted read-only: $opts"
  else echo "WARNING: $MOUNT is mounted read-write; AeroChorus never writes, but ro is safer"; fi
  if touch "$MOUNT/.aerochorus-write-probe" 2>/dev/null; then
    rm -f "$MOUNT/.aerochorus-write-probe"
    echo "WARNING: the mount accepted a write; switch it to 'ro' in /etc/fstab"
  else
    echo "write probe refused (good)"
  fi
  local first; first="$(find "$MOUNT" -maxdepth 4 -type f -name '*.mp3' -print -quit)"
  [[ -n "$first" ]] || die "no .mp3 files visible under $MOUNT"
  head -c 4096 "$first" >/dev/null && echo "read a real segment: ${first#"$MOUNT"/}"
}

phase_driver() {
  say "5-7. NVIDIA driver, GPU and CUDA 12 requirement"
  command -v nvidia-smi >/dev/null || die "NVIDIA driver not installed (nvidia-smi missing)"
  nvidia-smi --query-gpu=name,compute_cap,memory.total,driver_version --format=csv
  local cc; cc="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1 | xargs)"
  if [[ "$cc" != "6.1" ]]; then echo "NOTE: expected a GTX 1070 (6.1); found $cc. Profile follows detection."; fi
  if awk "BEGIN{exit !($cc < 7.5)}"; then
    echo "compute capability $cc < 7.5: CUDA 13 builds are unusable here; the CUDA 12 build is pinned"
  fi
}

phase_crispasr() {
  say "8-12. pinned CUDA 12 CrispASR"
  bash "$HERE/install-crispasr.sh" --prefix "$DATA" || {
    echo "release binary failed verification; trying a source build for sm_61"
    bash "$HERE/install-crispasr.sh" --prefix "$DATA" --build-from-source
  }
  cat "$DATA/crispasr/current/INSTALLED.json"
}

phase_python() {
  say "Python environment (uv)"
  command -v uv >/dev/null || { curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$PATH"; }
  (cd "$REPO" && uv sync --frozen)
}

phase_config() {
  say "configuration: $ETC/worker.toml, $ETC/aerochorus.env, $REPO/.env"
  sudo mkdir -p "$ETC"
  # shellcheck disable=SC1091
  . "$DATA/crispasr/current.env"
  if [[ ! -f "$ETC/worker.toml" ]]; then
    sed -e "s|@DATA@|$DATA|g" -e "s|@MOUNT@|$MOUNT|g" -e "s|@SOURCE_KEY@|$SOURCE_KEY|g" \
        -e "s|@CRISPASR_BINARY@|$CRISPASR_BINARY|g" -e "s|@LD_LIBRARY_PATH@|$LD_LIBRARY_PATH|g" \
        -e "s|@WORKER_NAME@|$(hostname -s)-worker|g" \
        "$HERE/worker.linux.toml.template" | sudo tee "$ETC/worker.toml" >/dev/null
    echo "wrote $ETC/worker.toml"
  else
    echo "$ETC/worker.toml exists; left unchanged"
  fi
  if [[ ! -f "$ETC/aerochorus.env" ]]; then
    sed -e "s|@DATA@|$DATA|g" -e "s|@REPO@|$REPO|g" -e "s|@LD_LIBRARY_PATH@|$LD_LIBRARY_PATH|g" \
        "$HERE/aerochorus.env.example" | sudo tee "$ETC/aerochorus.env" >/dev/null
    sudo chmod 640 "$ETC/aerochorus.env"; sudo chown root:"$RUN_USER" "$ETC/aerochorus.env"
  fi
  if [[ ! -f "$REPO/.env" ]]; then
    cat >"$REPO/.env" <<EOF
# Compose (Linux host). PostgreSQL stays on loopback; the API is local to this
# host unless remote workers need it (then set AEROCHORUS_API_BIND to the LAN IP).
AEROCHORUS_DATA=$DATA
AEROCHORUS_API_BIND=127.0.0.1
# Optional OpenSky credentials (control plane only; never sent to browsers):
# AEROCHORUS_OPENSKY_USERNAME=
# AEROCHORUS_OPENSKY_PASSWORD=
# AEROCHORUS_OPENSKY_CLIENT_ID=
# AEROCHORUS_OPENSKY_CLIENT_SECRET=
# (The OpenRouter key belongs in /etc/aerochorus/aerochorus.env, for the adjudicator.)
EOF
    chmod 600 "$REPO/.env"
  fi
}

compose() { (cd "$REPO" && docker compose -f docker-compose.yml -f deploy/linux/native/compose.linux.yml "$@"); }

phase_services() {
  say "13. PostgreSQL + API (Docker Compose, restart unless-stopped)"
  command -v docker >/dev/null || die "docker not installed"
  sudo systemctl enable --now docker >/dev/null 2>&1 || true
  compose up -d --build
  for _ in $(seq 60); do curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1 && break; sleep 2; done
  curl -fsS http://127.0.0.1:8000/health; echo
  export AEROCHORUS_WORKER_CONFIG="$ETC/worker.toml"
  uvx aerochorus models sync
  if ! uvx aerochorus source show "$SOURCE_KEY" >/dev/null 2>&1; then
    uvx aerochorus source add "$SOURCE_KEY" --name "Home ATC archive (BWI)" \
      --parser rtlsdr_airband --timezone America/New_York
  fi
}

phase_ui() {
  say "web UI build (Node in a throwaway container; no host Node needed)"
  docker run --rm -v "$REPO/ui:/ui" -w /ui -u "$(id -u):$(id -g)" -e HOME=/tmp node:24-slim \
    sh -c "npm ci --no-audit --no-fund && npm run build"
}

phase_systemd() {
  say "14. systemd: worker, review edge (LAN UI), nightly backup"
  for unit in aerochorus-worker.service aerochorus-edge.service aerochorus-backup.service aerochorus-backup.timer aerochorus-adjudicator.service; do
    sed -e "s|@REPO@|$REPO|g" -e "s|@USER@|$RUN_USER|g" -e "s|@DATA@|$DATA|g" \
        "$HERE/systemd/$unit" | sudo tee "/etc/systemd/system/$unit" >/dev/null
  done
  sudo systemctl daemon-reload
  sudo systemctl enable --now aerochorus-edge.service aerochorus-worker.service aerochorus-backup.timer
  systemctl --no-pager --lines=0 status aerochorus-edge.service aerochorus-worker.service || true
  # Installed, not enabled: it calls a paid API (for confirmed batches only).
  say "   adjudicator installed but not enabled; after setting AEROCHORUS_OPENROUTER_API_KEY in"
  say "   /etc/aerochorus/aerochorus.env: sudo systemctl enable --now aerochorus-adjudicator.service"
}

phase_smoke() {
  say "15-17. smoke: one CUDA backend + one CPU backend on real segments, tiny sweep, persistence"
  export AEROCHORUS_WORKER_CONFIG="$ETC/worker.toml"
  uvx aerochorus worker inventory --out "$DATA/logs/worker-inventory.json" >/dev/null
  uvx aerochorus worker qualify --suite smoke-linux1070 --n 5 --seed 1 --pull
  uvx aerochorus models qualifications
  # A tiny, explicit sweep. The worker service claims it; nothing else is queued.
  local before; before="$(compose exec -T postgres psql -U aerochorus -d aerochorus -Atc 'select count(*) from transcription_result')"
  uvx aerochorus sweep create --suite smoke-linux1070 --source "$SOURCE_KEY" --limit 5 \
    --name "linux1070 smoke" --allow-unqualified
  for _ in $(seq 120); do
    local after; after="$(compose exec -T postgres psql -U aerochorus -d aerochorus -Atc 'select count(*) from transcription_result')"
    [[ "$after" -ge $((before + 10)) ]] && break; sleep 5
  done
  echo "transcription results: $before -> $after (expected +10: 5 segments x 2 models)"
  [[ "$after" -ge $((before + 10)) ]] || die "the smoke sweep did not complete; see journalctl -u aerochorus-worker"
}

phase_report() {
  say "18. status"
  systemctl is-active aerochorus-edge.service aerochorus-worker.service docker || true
  compose ps
  echo
  echo "AeroChorus review UI:  http://$LAN_IP:8080/review   (also http://$(hostname).local:8080 if mDNS)"
  echo "API (local):           http://127.0.0.1:8000/docs"
  echo "Next: run the one-hour qualification corpus (docs/deployment/LINUX_DEPLOYMENT_RUNBOOK.md §6);"
  echo "      production sweeps start only when you create them."
}

for phase in "${PHASES[@]}"; do
  if [[ -z "$ONLY" || "$ONLY" == "$phase" ]]; then "phase_$phase"; fi
done
