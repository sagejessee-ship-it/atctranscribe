#!/usr/bin/env bash
# Import a Windows export bundle (deploy/migrate/export-windows.ps1) into the
# container deployment, then verify it (docs/deployment/MIGRATION.md).
#
#   aerochorus deploy must be up first:   ./deploy.sh up
#   then:                                 ./import.sh /path/to/migration-<stamp>
#
# 1 checksums  2 stop the app containers  3 recreate an empty database
# 4 pg_restore  5 compare every table count + content fingerprint with the export
# 6 start again (migrations bring the schema to head)  7 unpack AeroChorus-owned files
# Refuses to replace a database that already holds segments.
set -euo pipefail

BUNDLE="$(cd "${1:?usage: import.sh <export bundle dir>}" && pwd)"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${AEROCHORUS_DATA:-/srv/aerochorus}"
ENVF="$DATA/config/aerochorus.env"
compose() { docker compose -p "${AEROCHORUS_PROJECT:-aerochorus}" --env-file "$ENVF" -f "$DATA/deploy/compose.yml" "$@"; }
psql_() { compose exec -T postgres psql -v ON_ERROR_STOP=1 -U aerochorus "$@"; }
COUNTS="$DATA/deploy/counts.sql"
[[ -f "$COUNTS" ]] || COUNTS="$HERE/counts.sql"

echo "1/7 checksums"
(cd "$BUNDLE" && tr -d '\r' <SHA256SUMS | sha256sum -c -)

echo "2/7 stop everything but PostgreSQL"
compose stop api edge worker adjudicator backup 2>/dev/null || true
compose up -d postgres
for _ in $(seq 60); do compose exec -T postgres pg_isready -U aerochorus -d aerochorus >/dev/null 2>&1 && break; sleep 2; done
existing="$(psql_ -d aerochorus -Atc "select count(*) from segment" 2>/dev/null || echo 0)"
if [[ "${existing:-0}" != "0" ]]; then
  echo "the database already has $existing segments; refusing to overwrite it" >&2
  exit 1
fi

echo "3/7 recreate an empty database"
psql_ -d postgres -c "DROP DATABASE IF EXISTS aerochorus WITH (FORCE)" -c "CREATE DATABASE aerochorus OWNER aerochorus"

echo "4/7 restore"
compose cp "$BUNDLE/aerochorus.dump" postgres:/tmp/aerochorus.dump
compose exec -T postgres pg_restore -U aerochorus -d aerochorus --no-owner --exit-on-error /tmp/aerochorus.dump
compose exec -T postgres rm -f /tmp/aerochorus.dump

echo "5/7 verify counts + fingerprints against the export"
psql_ -d aerochorus -At -F $'\t' -f - <"$COUNTS" >"$BUNDLE/counts.linux.tsv"
# (Windows PowerShell may write a UTF-8 byte-order mark and CRLF line endings.)
if diff <(sed '1s/^\xEF\xBB\xBF//' "$BUNDLE/counts.tsv" | tr -d '\r') "$BUNDLE/counts.linux.tsv"; then
  echo "identical: every table count and fingerprint matches"
else
  echo "MISMATCH between export and restore; see the diff above" >&2
  exit 1
fi

echo "6/7 start (migrations bring the schema to head)"
compose up -d --remove-orphans
for _ in $(seq 90); do curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1 && break; sleep 2; done
curl -fsS http://127.0.0.1:8000/health; echo

echo "7/7 AeroChorus-owned files"
if [[ -f "$BUNDLE/artifacts-jesseepc.tgz" ]]; then
  tar -xzf "$BUNDLE/artifacts-jesseepc.tgz" -C "$DATA/artifacts-jesseepc" --strip-components=1
  chmod -R a-w "$DATA/artifacts-jesseepc"   # historical raw output: read-only from now on
fi
if [[ -f "$BUNDLE/atco2_fixed.tgz" ]]; then
  tar -xzf "$BUNDLE/atco2_fixed.tgz" -C "$DATA/corpora"
fi
echo "done. Next: aerochorus worker models pull --suite smoke-linux1070 (see the runbook)"
