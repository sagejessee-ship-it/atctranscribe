#!/usr/bin/env bash
# AeroChorus backup (docs/deployment/BACKUP.md).
#
# Critical, non-re-creatable data lives in PostgreSQL (human corrections, span
# annotations, dataset manifests, model pedigree, the links from labels to
# evidence), so the database dump is the backup that matters. Also kept:
# dataset exports and the context cache. Re-creatable data (models, caches,
# UI builds) is not backed up. Raw ASR artifacts are expensive but re-creatable:
# included when AEROCHORUS_BACKUP_ARTIFACTS=1.
#
# Copy $DATA/backups off this machine regularly (another disk or the 5080 box);
# never onto the collector's archive share.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA="${AEROCHORUS_DATA:-/srv/aerochorus}"
KEEP_DAYS="${AEROCHORUS_BACKUP_KEEP_DAYS:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$DATA/backups/$STAMP"
mkdir -p "$DEST"

compose() { (cd "$REPO" && docker compose -f docker-compose.yml -f deploy/linux/compose.linux.yml "$@"); }

# 1. PostgreSQL: custom-format dump (restorable table-by-table with pg_restore).
compose exec -T postgres pg_dump -U aerochorus -d aerochorus -Fc >"$DEST/aerochorus.dump"
# Row counts for verification after a restore.
compose exec -T postgres psql -U aerochorus -d aerochorus -At -F $'\t' -f - \
  <"$REPO/deploy/migrate/counts.sql" >"$DEST/counts.tsv"

# 2. Critical files outside the database.
tar -C "$DATA" -czf "$DEST/exports-context.tgz" exports context 2>/dev/null || true
cp /etc/aerochorus/worker.toml "$DEST/" 2>/dev/null || true
if [[ "${AEROCHORUS_BACKUP_ARTIFACTS:-0}" == "1" ]]; then
  tar -C "$DATA" -czf "$DEST/artifacts.tgz" artifacts
fi

(cd "$DEST" && sha256sum ./* >SHA256SUMS)
find "$DATA/backups" -mindepth 1 -maxdepth 1 -type d -mtime +"$KEEP_DAYS" -exec rm -rf {} +
echo "backup written: $DEST ($(du -sh "$DEST" | cut -f1))"
