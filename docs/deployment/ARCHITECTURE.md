# Deployment architecture (ADR-021)

```text
ALIENWARE COLLECTOR (unchanged, independent)
──────────────────────────────────────────────
SDR / RTLSDR-Airband capture → segmentation → immutable MP3 archive
share: //192.168.68.84/bwi
        │
        │  LAN, SMB/CIFS, mounted READ-ONLY at /mnt/aerochorus/atc
        ▼
LINUX AEROCHORUS HOST: GTX 1070 8 GB (Pascal, CC 6.1), 32 GB RAM
──────────────────────────────────────────────────────────────────
Docker Compose (restart: unless-stopped)
  postgres:17        127.0.0.1:5432 only   data → /srv/aerochorus/postgres
  migrate            one-shot alembic upgrade
  api (FastAPI)      127.0.0.1:8000        (LAN only if remote workers need it)

systemd (native, User=<operator>)
  aerochorus-edge    0.0.0.0:8080  web UI + /api proxy + read-only /audio (sha256-checked)
  aerochorus-worker  heartbeats, scans, queued sweeps
      └─ launches the pinned CrispASR CUDA 12 binary, ONE model resident at a time
         /srv/aerochorus/crispasr/current → 0.8.37 (sha256 recorded)
  aerochorus-backup.timer   nightly pg_dump + critical files

/srv/aerochorus/  postgres/ models/ artifacts/ artifacts-jesseepc/ exports/
                  cache/ context/ corpora/atco2_fixed/ logs/ backups/ crispasr/ cuda12/
        │
        ▼  HTTP :8080 (trusted LAN, no auth, never port-forwarded)
LAN BROWSERS: Intel Mac, desktop PCs, other devices

OPTIONAL EXTRA WORKER: Windows RTX 5080 box
  worker (Docker CrispASR CUDA image) → http://<linux-host>:8000 when AEROCHORUS_API_BIND=<LAN IP>
  profile windows_blackwell_16gb; ATCO2 benchmark source available there too
```

## What owns what

| data | where | category (BACKUP.md) |
| --- | --- | --- |
| source audio | collector share, read-only | not AeroChorus's to back up |
| segments, results, agreement, annotations, datasets, pedigree, qualification, airports, ADS-B snapshots | PostgreSQL | critical (annotations, datasets, pedigree) / expensive (results) |
| raw CrispASR responses | `/srv/aerochorus/artifacts` (`artifact://linux1070/…`), `artifacts-jesseepc` (historical) | expensive but re-creatable |
| model files | `/srv/aerochorus/models` (pinned sha256) | re-creatable |
| dataset exports (clips + manifest) | `/srv/aerochorus/exports` | re-creatable from the DB + audio, still backed up |
| CrispASR binary + CUDA 12 runtime | `/srv/aerochorus/crispasr`, `/srv/aerochorus/cuda12` | re-creatable (`crispasr.lock`) |

## Identity rules that survive the move

- A segment is `(logical source key, relative path)`. The mount path is
  configuration in `worker.toml` only, and is never stored as identity.
- Raw artifacts are `artifact://<store>/…`. The Windows store name
  (`jesseepc`) is kept and mapped with `artifact_mounts`. New results use the
  `linux1070` store.
- A hardware profile (e.g. `linux_pascal_8gb`) is derived from the
  hardware, never from a hostname. Qualification is recorded per
  (model, profile).
