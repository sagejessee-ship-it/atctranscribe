# Data migration: Windows dev box → Linux host

State today (2026-09-27) lives on the Windows RTX 5080 box. It is a real
research database, not throwaway dev data, so it is migrated. Nothing is
invented: the steps below are scripted, and the dump/restore round trip has
been verified.

| item | source (Windows) | size | action |
| --- | --- | --- | --- |
| PostgreSQL | Docker volume `aerochorus_pgdata` | 516 MB in the DB, 50 MB custom-format dump | `pg_dump -Fc` → `pg_restore`, verified by counts and fingerprints |
| raw CrispASR artifacts | `%USERPROFILE%\aerochorus-data\artifacts` (`artifact://jesseepc/…`) | 122 MB (31 MB tgz) | copied to `/srv/aerochorus/artifacts-jesseepc`, read-only, mapped via `artifact_mounts` |
| ATCO2 benchmark clips (AeroChorus-derived) | `…\corpora\atco2_fixed` | 112 MB | copied to `/srv/aerochorus/corpora/atco2_fixed` |
| model files | `…\models` | 8.6 GB | **not copied**: re-downloaded and verified by sha256 on Linux |
| collector audio | `\\192.168.68.84\bwi` | ~1 TB | **never copied**: mounted read-only |

Contents on 2026-09-27: 448,899 segments; 7 models (now 18 in the catalog);
6 sweeps; 27,823 transcription results; 877 ATCO2 gold references;
1 human annotation; 1 airport profile (KBWI); 5,790 agreement rows.

## 1. Export (Windows)

```powershell
cd C:\Users\sagej\Projects\atctranscribe
powershell -ExecutionPolicy Bypass -File deploy\migrate\export-windows.ps1
```

This writes `%USERPROFILE%\aerochorus-data\migration\<stamp>\` containing
`aerochorus.dump`, `counts.tsv` (row counts plus content fingerprints of the
critical tables), `alembic_version.txt`, `artifacts-jesseepc.tgz`,
`atco2_fixed.tgz` and `SHA256SUMS`.

Stop writing on Windows first: no running sweep, and no review session
saving annotations. Otherwise newer rows are left behind.

## 2. Copy

```powershell
scp -r "$env:USERPROFILE\aerochorus-data\migration\<stamp>" <user>@<linux-host>:/srv/aerochorus/backups/migration-<stamp>
```

## 3. Import (Linux), after `host-setup.sh` and `deploy.sh up`

The containers must be deployed first
([LINUX_DEPLOYMENT_RUNBOOK.md](LINUX_DEPLOYMENT_RUNBOOK.md) §3–4). A fresh
deployment has an empty database.

```bash
/srv/aerochorus/deploy/import.sh /srv/aerochorus/backups/migration-<stamp>
```

The import:

1. verifies the checksums;
2. stops everything but PostgreSQL, and refuses a database that already has
   segments;
3. recreates an empty database, so the dump's schema version is restored
   exactly;
4. runs `pg_restore`;
5. **compares every count and fingerprint** with the export (a UTF-8 BOM or
   CRLF from Windows PowerShell is tolerated);
6. starts everything again; migrations bring the schema to head;
7. unpacks the artifacts (read-only) and the ATCO2 clips.

Then pull models (`aerochorus worker models pull --suite smoke-linux1070`)
and qualify (runbook §10).

## 4. After

- Keep the Windows database untouched for a while as a fallback, but do not
  run both control planes against the same collector with workers writing
  to both.
- The 5080 box can become a second worker for the Linux API (set
  `AEROCHORUS_API_BIND=<LAN IP>` on Linux and `api_url` on Windows). It
  keeps the `jesseepc` artifact store for any new results it produces.

## Verified

2026-09-27: the export ran on Windows, producing a 186 MB bundle. The dump
was restored into a scratch database and **all 13 tables' counts and
fingerprints were identical** (`deploy/migrate/counts.sql`). Re-run the
export on migration day: the database keeps changing until then.
