#!/usr/bin/env bash
# Import an export-windows.ps1 bundle on the Linux host, then verify it.
#
#   deploy/linux/native/import-linux.sh /srv/aerochorus/backups/migration-<stamp>
#
# Order: checksums -> empty DB with Postgres only -> pg_restore -> migrate to head
# -> counts/fingerprints compared -> artifacts + ATCO2 clips unpacked.
# Refuses to restore over a database that already has segments.
set -euo pipefail

BUNDLE="$(cd "${1:?usage: import-linux.sh <bundle dir>}" && pwd)"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
DATA="${AEROCHORUS_DATA:-/srv/aerochorus}"
compose() { (cd "$REPO" && docker compose -f docker-compose.yml -f deploy/linux/native/compose.linux.yml "$@"); }
psql_() { compose exec -T postgres psql -U aerochorus -d aerochorus "$@"; }

echo "1/6 checksums"
(cd "$BUNDLE" && sha256sum -c SHA256SUMS)

echo "2/6 PostgreSQL only"
compose up -d postgres
for _ in $(seq 60); do compose exec -T postgres pg_isready -U aerochorus -d aerochorus >/dev/null 2>&1 && break; sleep 2; done
existing="$(psql_ -Atc "select count(*) from segment" 2>/dev/null || echo 0)"
if [[ "${existing:-0}" != "0" ]]; then
  echo "the target database already has $existing segments; refusing to overwrite" >&2
  exit 1
fi

echo "3/6 restore"
compose cp "$BUNDLE/aerochorus.dump" postgres:/tmp/aerochorus.dump
compose exec -T postgres pg_restore -U aerochorus -d aerochorus --clean --if-exists --no-owner /tmp/aerochorus.dump
compose exec -T postgres rm -f /tmp/aerochorus.dump

echo "4/6 verify counts + fingerprints against the export"
psql_ -At -F $'\t' -f - <"$REPO/deploy/migrate/counts.sql" >"$BUNDLE/counts.linux.tsv"
if diff <(tr -d '\r' <"$BUNDLE/counts.tsv") "$BUNDLE/counts.linux.tsv"; then
  echo "identical: every table count and fingerprint matches"
else
  echo "MISMATCH between export and restore; see the diff above" >&2
  exit 1
fi

echo "5/6 bring the schema to head (API up)"
compose up -d --build
for _ in $(seq 60); do curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1 && break; sleep 2; done
curl -fsS http://127.0.0.1:8000/health; echo

echo "6/6 AeroChorus-owned files"
mkdir -p "$DATA/artifacts-jesseepc" "$DATA/corpora"
tar -xzf "$BUNDLE/artifacts-jesseepc.tgz" -C "$DATA/artifacts-jesseepc" --strip-components=1
[[ -f "$BUNDLE/atco2_fixed.tgz" ]] && tar -xzf "$BUNDLE/atco2_fixed.tgz" -C "$DATA/corpora"
chmod -R a-w "$DATA/artifacts-jesseepc"   # historical raw output: read-only from now on
echo "done. Models re-download with: uv run aerochorus worker models pull --suite smoke-linux1070"
