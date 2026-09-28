#!/usr/bin/env bash
# AeroChorus backup (docs/deployment/BACKUP.md). Runs in the `backup` container
# (postgres:17 image) from compose.yml:
#   bash /deploy/backup.sh --loop   nightly at $BACKUP_HOUR (container default)
#   bash /deploy/backup.sh          once, now (deploy.sh backup)
#
# The database dump is the backup that matters: human corrections, spans, dataset
# manifests, model pedigree and the links from labels to evidence live only there.
# Also kept: the config (secrets included, so protect the backup directory) and
# dataset exports. Models, caches and images are re-creatable and not backed up.
# Copy $AEROCHORUS_DATA/backups off this machine regularly; never onto the
# collector's archive share.
set -euo pipefail

KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"

backup_once() {
  local stamp dest
  stamp="$(date +%Y%m%d-%H%M%S)"
  dest="/backups/$stamp"
  mkdir -p "$dest"
  # 1. PostgreSQL: custom format (pg_restore can restore table by table).
  pg_dump -Fc -f "$dest/aerochorus.dump"
  # Row counts + content fingerprints, to verify a restore (deploy/migrate/counts.sql).
  psql -At -F $'\t' -f /deploy/counts.sql >"$dest/counts.tsv"
  # 2. Critical files outside the database.
  tar -C /data -czf "$dest/config-exports.tgz" config exports 2>/dev/null || true
  (cd "$dest" && sha256sum ./* >SHA256SUMS)
  chmod -R go-rwx "$dest"
  find /backups -mindepth 1 -maxdepth 1 -type d -mtime +"$KEEP_DAYS" -exec rm -rf {} +
  echo "$(date -Is) backup written: $dest ($(du -sh "$dest" | cut -f1))"
}

if [[ "${1:-}" != "--loop" ]]; then
  backup_once
  exit 0
fi

hour="${BACKUP_HOUR:-3}"
echo "$(date -Is) nightly backups at ${hour}:00 $(date +%Z), keeping ${KEEP_DAYS} days"
while true; do
  now="$(date +%s)"
  next="$(date -d "today ${hour}:00" +%s)"
  (( next > now )) || next="$(date -d "tomorrow ${hour}:00" +%s)"
  sleep $(( next - now ))
  backup_once || echo "$(date -Is) BACKUP FAILED" >&2
done
