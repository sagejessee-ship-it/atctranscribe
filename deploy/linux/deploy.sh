#!/usr/bin/env bash
# AeroChorus on the Linux GPU host: containers only (ADR-023).
# Run from an unpacked deploy bundle (the first time and for every update), or as
# $AEROCHORUS_DATA/deploy/deploy.sh afterwards. Needs Docker access, not root.
#
#   bash deploy.sh up           install this bundle: load its images, copy the kit to
#                               $DATA/deploy, write config on the first run, start
#                               everything, smoke-test. Also the update command.
#   ./deploy.sh status          containers, health, where the UI is
#   ./deploy.sh smoke           re-run the checks (API, UI, source mount, GPU, CrispASR, worker)
#   ./deploy.sh logs [service]  follow logs (all, or one: api edge worker ...)
#   ./deploy.sh backup          one database backup now
#   ./deploy.sh restart [svc]   restart everything or one service
#   ./deploy.sh down            stop everything (data and config stay)
#   ./deploy.sh compose ARGS    any `docker compose` command against this deployment
#
# One-off CLI commands: `aerochorus <command>` (a wrapper: runs the CLI in a container).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${AEROCHORUS_DATA:-/srv/aerochorus}"
CONF="$DATA/config"
ENVF="$CONF/aerochorus.env"
KIT="$DATA/deploy"
MOUNT="${AEROCHORUS_CORPUS_MOUNT:-/mnt/aerochorus/atc}"
KIT_FILES=(compose.yml deploy.sh aerochorus backup.sh import.sh host-setup.sh inventory.sh
           counts.sql aerochorus.env.example worker.toml.example fstab.example crispasr.lock
           README.md IMAGES.txt)

say() { printf '\n== %s\n' "$*"; }
ok() { printf '   ok: %s\n' "$*"; }
fail() { printf '   FAIL: %s\n' "$*"; FAILED=1; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

compose() {
  [[ -f "$ENVF" ]] || die "no $ENVF yet: run bash deploy.sh up from a deploy bundle first"
  local profiles=()
  grep -q '^AEROCHORUS_ADJUDICATOR=1' "$ENVF" && profiles+=(--profile adjudication)
  docker compose -p "${AEROCHORUS_PROJECT:-aerochorus}" --env-file "$ENVF" -f "$KIT/compose.yml" "${profiles[@]}" "$@"
}
env_value() { sed -n "s/^$1=//p" "$ENVF" | tail -1; }

bundle_tag() { sed -n 's/^TAG=//p' "$HERE/IMAGES.txt" 2>/dev/null | head -1; }

install_kit() {
  say "1. bundle"
  docker info >/dev/null 2>&1 || die "cannot talk to Docker (run host-setup.sh; log in again after being added to the docker group)"
  [[ -d "$DATA" ]] || die "$DATA does not exist: run sudo bash host-setup.sh first"
  if [[ -f "$HERE/SHA256SUMS" ]]; then
    (cd "$HERE" && sha256sum --quiet -c SHA256SUMS) || die "bundle checksums do not match (incomplete copy?)"
    ok "bundle checksums verified"
  fi
  TAG="$(bundle_tag)"
  [[ -n "$TAG" ]] || die "no IMAGES.txt with TAG=: run this from a deploy bundle made by package.ps1"
  if [[ "$HERE" != "$KIT" ]]; then
    if [[ -f "$KIT/compose.yml" ]]; then
      rm -rf "$KIT.prev"
      cp -a "$KIT" "$KIT.prev"
    fi
    mkdir -p "$KIT"
    for f in "${KIT_FILES[@]}"; do
      [[ -f "$HERE/$f" ]] && install -m "$([[ $f == *.sh || $f == aerochorus ]] && echo 755 || echo 644)" "$HERE/$f" "$KIT/$f"
    done
    ok "kit $TAG installed in $KIT (previous kit in $KIT.prev)"
  fi
}

load_images() {
  say "2. images $TAG"
  if docker image inspect "aerochorus-app:$TAG" "aerochorus-worker:$TAG" >/dev/null 2>&1; then
    ok "already loaded"
    return
  fi
  local archive
  archive="$(ls "$HERE"/images-*.tar.gz 2>/dev/null | head -1)"
  [[ -n "$archive" ]] || die "images for $TAG are not loaded and no images-*.tar.gz is in $HERE"
  echo "   loading $(basename "$archive") ($(du -h "$archive" | cut -f1)); this takes a few minutes"
  docker load -i "$archive"
  docker image inspect "aerochorus-app:$TAG" "aerochorus-worker:$TAG" >/dev/null
  ok "loaded"
}

write_config() {
  say "3. config in $CONF"
  mkdir -p "$CONF"
  chmod 700 "$CONF"
  for d in postgres models artifacts artifacts-jesseepc corpora exports backups logs cache; do
    mkdir -p "$DATA/$d"
  done
  if [[ ! -f "$ENVF" ]]; then
    local pw
    pw="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    sed -e "s#@TAG@#$TAG#" -e "s#@DATA@#$DATA#" -e "s#@MOUNT@#$MOUNT#" -e "s#@PGPASSWORD@#$pw#" \
      -e "s#@UID@#$(id -u)#" -e "s#@GID@#$(id -g)#" \
      "$KIT/aerochorus.env.example" >"$ENVF"
    chmod 600 "$ENVF"
    ok "created $ENVF (random database password). Add OpenSky / OpenRouter keys there if wanted."
  else
    sed -i "s#^AEROCHORUS_TAG=.*#AEROCHORUS_TAG=$TAG#" "$ENVF"
    ok "AEROCHORUS_TAG=$TAG in $ENVF"
  fi
  if [[ ! -f "$CONF/worker.toml" ]]; then
    install -m 644 "$KIT/worker.toml.example" "$CONF/worker.toml"
    ok "created $CONF/worker.toml"
  fi
  compose config -q || die "compose configuration is invalid (see above)"
}

wait_healthy() {
  local url="$1" tries="${2:-90}"
  for _ in $(seq "$tries"); do
    curl -fsS "$url" >/dev/null 2>&1 && return 0
    sleep 2
  done
  return 1
}

smoke() {
  say "smoke checks"
  FAILED=0
  local edge_port api
  edge_port="$(env_value AEROCHORUS_EDGE_PORT)"; edge_port="${edge_port:-8080}"
  api="http://127.0.0.1:$(env_value AEROCHORUS_API_PORT | grep . || echo 8000)"
  if wait_healthy "$api/health" 60; then ok "API healthy"; else fail "API not healthy (./deploy.sh logs api)"; fi
  if wait_healthy "http://127.0.0.1:$edge_port/edge/health" 30; then
    local health
    health="$(curl -fsS "http://127.0.0.1:$edge_port/edge/health")"
    if grep -q '"home_atc_archive":{"available":true' <<<"$health"; then
      ok "web UI up; collector archive readable"
    else
      fail "web UI up, but the archive is not readable in the container: $health"
    fi
  else
    fail "web UI not answering on :$edge_port (./deploy.sh logs edge)"
  fi
  if compose exec -T worker nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader; then
    ok "worker container sees the GPU"
  else
    fail "worker container cannot see the GPU (NVIDIA Container Toolkit?)"
  fi
  if compose exec -T worker aerochorus worker crispasr check >/tmp/aerochorus-crispasr.txt 2>&1; then
    ok "CrispASR runtime: $(head -3 /tmp/aerochorus-crispasr.txt | tr '\n' ' ')"
  else
    fail "CrispASR check failed: $(tail -5 /tmp/aerochorus-crispasr.txt | tr '\n' ' ')"
  fi
  local name
  name="$(sed -n 's/^worker_name *= *"\(.*\)"/\1/p' "$CONF/worker.toml")"
  for _ in $(seq 20); do
    curl -fsS "$api/api/v1/workers" | grep -q "\"name\":\"$name\"" && break
    sleep 3
  done
  if curl -fsS "$api/api/v1/workers" | grep -q "\"name\":\"$name\""; then
    ok "worker $name reports to the API"
  else
    fail "worker $name has not sent a heartbeat yet (./deploy.sh logs worker)"
  fi
  local ip
  ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  echo
  echo "   Review UI: http://${ip:-<this host>}:$edge_port/review   (trusted LAN only)"
  (( FAILED == 0 )) || { echo "   some checks failed"; return 1; }
}

cmd="${1:-status}"; shift || true
case "$cmd" in
  up)
    install_kit
    load_images
    write_config
    say "4. start"
    compose up -d --remove-orphans
    smoke
    ;;
  status)
    compose ps
    echo
    grep -q '^AEROCHORUS_ADJUDICATOR=1' "$ENVF" && echo "adjudicator: enabled" || echo "adjudicator: off"
    echo "images: $(env_value AEROCHORUS_TAG)"
    ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
    echo "review UI: http://${ip:-<this host>}:$(env_value AEROCHORUS_EDGE_PORT)/review"
    ;;
  smoke) smoke ;;
  logs) compose logs -f --tail 200 "$@" ;;
  backup) compose exec -T backup bash /deploy/backup.sh ;;
  restart) compose restart "$@" ;;
  down) compose down ;;
  compose) compose "$@" ;;
  *) sed -n '2,20p' "$0"; exit 2 ;;
esac
