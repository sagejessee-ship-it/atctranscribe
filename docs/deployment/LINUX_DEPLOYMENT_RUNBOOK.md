# Linux GTX 1070 deployment runbook: containers only (ADR-021, ADR-023)

The Linux desktop runs AeroChorus **as containers only**. There is no
repository, Python, Node or editor on it. Everything is built on the Windows
PC and shipped as one folder (a *deploy bundle*). The diagram is in
[ARCHITECTURE.md](ARCHITECTURE.md), and data migration is in
[MIGRATION.md](MIGRATION.md). Record results in
[LINUX_GTX1070_MIGRATION_REPORT.md](LINUX_GTX1070_MIGRATION_REPORT.md).

```text
Windows PC (dev)                         Linux host (deploy target)
  package.ps1 ──► aerochorus-deploy-<tag>/ ──copy──►  host-setup.sh (once, sudo)
   builds images   images-<tag>.tar.gz                deploy.sh up   (install + every update)
                   kit: compose.yml, scripts,         /srv/aerochorus/{config,deploy,postgres,models,…}
                   templates, SHA256SUMS              review UI  http://<host>:8080/review
```

## 0. What the host needs

| item | notes |
| --- | --- |
| Ubuntu 22.04/24.04 (or Debian) | `host-setup.sh` uses apt |
| NVIDIA driver ≥ 570 | The worker image carries the CUDA 12.8 runtime. **580 is the last branch that supports Pascal**: never go past it. `host-setup.sh --install-driver` installs 580. |
| Docker Engine + Compose plugin | installed by `host-setup.sh` if missing (Frigate likely brought Docker already) |
| NVIDIA Container Toolkit | installed and configured by `host-setup.sh`, then proven with a GPU container |
| the collector share | mounted read-only at `/mnt/aerochorus/atc` by `host-setup.sh` |
| SSH from the PC (optional) | `ssh-copy-id <user>@192.168.68.53` makes copying the bundle easy |
| free disk | about 15 GB for images and models to start (all 18 models ≈ 20 GB), plus the database (≈ 1 GB) and artifacts |

## 1. Build the bundle (Windows PC)

```powershell
cd C:\Users\sagej\Projects\atctranscribe
powershell -ExecutionPolicy Bypass -File deploy\linux\package.ps1
```

This builds `aerochorus-app:<tag>` and `aerochorus-worker:<tag>`, where
`<tag>` is `<date>-<commit>`. The worker image contains the pinned CUDA 12
CrispASR, sha256-verified. The script saves both, plus `postgres:17`, into
`%USERPROFILE%\aerochorus-data\deploy\aerochorus-deploy-<tag>\` together with
the kit and checksums. That is about 2 GB compressed, and the save takes a
few minutes.

## 2. Copy it to the host

```powershell
scp -r "$env:USERPROFILE\aerochorus-data\deploy\aerochorus-deploy-<tag>" <user>@192.168.68.53:~/
```

A USB disk or a network share works too. `deploy.sh` verifies `SHA256SUMS`
before it uses anything.

## 3. Prepare the host (once)

```bash
cd ~/aerochorus-deploy-<tag>
sudo bash host-setup.sh --stop-frigate   # `bash`: copies from Windows lose the executable bit
```

Every step checks first, so the script is safe to re-run. In order, it:

1. **Inventory**: OS, CPU, RAM, GPU, driver and disk, written to
   `/srv/aerochorus/logs/inventory-*.json` (read-only).
2. **Driver**: requires ≥ 570. Without it, re-run with `--install-driver`,
   then reboot and run again.
3. **Docker Engine + Compose plugin**, and adds you to the `docker` group.
   Log out and back in before running `deploy.sh`.
4. **NVIDIA Container Toolkit**, then `docker run --gpus all … nvidia-smi -L`
   must list the GTX 1070.
5. **Frigate**: `--stop-frigate` stops it and disables its restart. Also
   lists GPU processes, and warns if ports 8080, 8000 or 5432 are taken.
6. **Share**: asks once for the share user and password. They are stored in
   `/etc/aerochorus/smb.cred`, root-only. It adds a read-only, `nofail`,
   automount line to `/etc/fstab` (see `fstab.example`), mounts the share,
   and **proves a write is refused**.
7. **`/srv/aerochorus`** directories, and the `aerochorus` CLI wrapper in
   `/usr/local/bin`.

Options: `--share //192.168.68.84/bwi`, `--mount /mnt/aerochorus/atc`,
`--data /srv/aerochorus`, and `--skip-mount`.

## 4. Deploy (first install and every update)

```bash
bash deploy.sh up          # from the bundle folder
```

The script:

1. verifies the bundle checksums;
2. installs the kit into `/srv/aerochorus/deploy`, keeping the old one in
   `deploy.prev`;
3. runs `docker load` for the images;
4. on the first run, writes `/srv/aerochorus/config/aerochorus.env` (random
   database password, chmod 600) and `/srv/aerochorus/config/worker.toml`;
5. starts the services;
6. runs the smoke checks:
   - the API is healthy;
   - the UI answers **and the archive is readable inside the container**;
   - the worker container sees the GPU;
   - the CrispASR identity is printed;
   - the worker has sent a heartbeat.

It ends with the review UI address.

| service | what | exposure |
| --- | --- | --- |
| `postgres` | PostgreSQL 17, data in `/srv/aerochorus/postgres` | 127.0.0.1:5432 only |
| `migrate` | applies migrations, then exits | — |
| `api` | control plane (FastAPI) | 127.0.0.1:8000 |
| `edge` | **review web UI**, API proxy, read-only sha256-checked audio | **LAN :8080** |
| `worker` | scans, heartbeats, queued sweeps; CrispASR on the GPU, one model at a time | — |
| `backup` | nightly `pg_dump` at 03:00 into `/srv/aerochorus/backups` | — |
| `adjudicator` | optional and paid (ADR-022); only if `AEROCHORUS_ADJUDICATOR=1` | outbound HTTPS |
| `cli` | one-off commands via `aerochorus …` | — |

There is no authentication. Keep the UI on the trusted LAN and never
port-forward it.

## 5. Data

**Migrating from the Windows PC** (the normal case; see
[MIGRATION.md](MIGRATION.md)). Export on Windows, copy the export folder,
then:

```bash
/srv/aerochorus/deploy/import.sh /srv/aerochorus/backups/migration-<stamp>
```

It restores into a fresh database, then **compares every table count and
fingerprint** with the export. It then brings the schema to head and unpacks
the old raw artifacts (read-only) and the ATCO2 clips.

**Starting empty instead:**

```bash
aerochorus source add home_atc_archive --name "Home ATC archive" --parser rtlsdr_airband --timezone America/New_York
aerochorus models sync
aerochorus airport bootstrap KBWI --timezone America/New_York --station BWI
aerochorus airport airspace KBWI
```

**Then, in both cases**, fetch models. They are downloaded and verified by
sha256:

```bash
aerochorus worker models pull --suite smoke-linux1070
```

## 6. Open the UI

Open `http://<linux-host-ip>:8080/review` from the Mac or any PC. Run
`/srv/aerochorus/deploy/deploy.sh status` to print the address.

## 7. Day-to-day operation

```bash
D=/srv/aerochorus/deploy/deploy.sh
$D status                 # containers, image tag, adjudicator on/off, UI address
$D smoke                  # the checks again
$D logs worker            # or api, edge, backup, adjudicator (Ctrl-C to stop following)
$D restart edge
$D backup                 # a backup now
$D down / $D up           # stop / start (data and config stay)
$D compose ps             # any docker compose command against this deployment
aerochorus worker health  # the CLI, in a one-off container
```

**Settings and secrets** live in `/srv/aerochorus/config/aerochorus.env`:
OpenSky, OpenRouter, ports and backup hour. Edit the file, then run `$D up`
to apply. **Worker settings** (sources, memory strategies) live in
`/srv/aerochorus/config/worker.toml`. Edit it, then run `$D restart worker`.

**Reboot test** (acceptance): run `sudo reboot`. Afterwards `$D status`
should show every service `Up`, the UI should answer, and `$D smoke` should
pass. The containers are `restart: unless-stopped`.

## 8. Updates and rollback

To update, make a new bundle on the PC, copy it, and run `bash deploy.sh up`
in it. Only the tag changes in `aerochorus.env`. The config, data and database
stay. Migrations run automatically.

To roll back, the previous images are still loaded:

```bash
sed -i 's/^AEROCHORUS_TAG=.*/AEROCHORUS_TAG=<previous tag>/' /srv/aerochorus/config/aerochorus.env
cp -a /srv/aerochorus/deploy.prev/. /srv/aerochorus/deploy/ && /srv/aerochorus/deploy/deploy.sh compose up -d
```

Rolling back across a migration needs a backup restore (see
[BACKUP.md](BACKUP.md)). Old images take space: remove them with
`docker image rm aerochorus-app:<old> aerochorus-worker:<old>`.

## 9. Memory strategy on 8 GB

Defaults: quantized weights (q4_k/q8_0 in the catalog), one model resident,
no offload. If a model OOMs or runs too close to 8 GB, give **only that
model** an override in `/srv/aerochorus/config/worker.toml`, escalating in
this order:

```toml
[transcription.model_runtime."canary-qwen-2.5b-q4_k"]
strategy = "kv-q8"                                  # 1. quantized KV
env = { CRISPASR_KV_QUANT = "q8_0" }
# strategy = "partial-24"                           # 2. partial layer residency
# env = { CRISPASR_N_GPU_LAYERS = "24" }
# strategy = "kv-cpu"                               # 3. KV on CPU
# env = { CRISPASR_KV_ON_CPU = "1" }
# last resort, manual experiment only: GGML_CUDA_ENABLE_UNIFIED_MEMORY = "1"
```

The strategy and its env are recorded with every run and qualification, and
they are part of the runtime fingerprint. Re-qualify after changing them.

`max_concurrent_models = 2` (default 1) lets a second model run at the same
time. It starts only when its estimated footprint fits in free GPU memory minus
`vram_headroom_mb`. The estimate comes from qualification on this profile, or
from the file size when there is none. On 8 GB only small pairs fit, such as
parakeet + whisper. Leave it at 1 until qualification has measured the models.

## 10. Qualification (before any production sweep)

Qualification needs the GPU, so it runs in the worker service's container
with the daemon stopped. The two never share 8 GB of VRAM.

```bash
D=/srv/aerochorus/deploy/deploy.sh
$D compose stop worker
# roster attempt: every candidate, 20 real segments (deterministic sample), pulls models as needed
$D compose run --rm worker aerochorus worker qualify --suite qualify-linux1070 --n 20 --seed 1 --pull \
    --utc-from 2026-09-08T12:00:00Z --utc-to 2026-09-09T00:00:00Z
# the "one-hour" corpus (~400 BWI segments):
$D compose run --rm worker aerochorus worker qualify --suite core-atc-linux1070 --n 400 --seed 2 \
    --utc-from 2026-09-08T00:00:00Z --utc-to 2026-09-09T00:00:00Z
$D compose start worker
aerochorus models qualifications --profile linux_pascal_8gb
```

States:

- `qualified`, `qualified_cpu_only`, `qualified_with_offload`;
- `too_slow` (RTF > 1.0 by default; see `--max-rtf`);
- `oom`;
- `backend_failure` (including "loads but returns nothing usable");
- `unsupported_on_platform`, `not_relevant`, `experimental`.

A worker on this profile never claims a model recorded as `oom`,
`backend_failure`, `unsupported_on_platform` or `not_relevant`. Such a model
never blocks the rest of a sweep.

Then decide, explicitly:

```bash
aerochorus models set <name> --eligible     # may full sweeps use it
```

Whether a model **votes** in agreement is decided in the web UI, on the
Transcribe page under **Model voting**. It shows each model's exact and
near-match rate against the other families' consensus, and how often it
produces words where the voters heard nothing. A decision needs a reason,
recomputes agreement for that model's segments, and is kept by `models sync`.

The `full-qualified-linux1070` suite is set in `config/models.toml` in the
repository. Change it on the PC, ship a new bundle, then run `aerochorus
models sync`.

## 11. Production backlog: only on explicit action

Nothing transcribes by itself. The worker only processes sweeps someone
queued.

1. Run `$D smoke`, then `aerochorus source summary home_atc_archive`.
2. Run the qualification (§10), and look at the results.
3. Queue the backlog yourself: on the web UI's **Transcribe** page (pick the
   days, the channels and the qualified models), or with
   `aerochorus sweep create --suite full-qualified-linux1070 …`.

## 12. Failure behaviour

| event | effect |
| --- | --- |
| collector offline or mount lost | scans and sweeps release work as `source_unavailable`; segments are never marked missing on an unavailable source; the UI audio shows 503 with the reason |
| worker container stopped or host rebooted mid-sweep | the worker stops on SIGINT and releases the model run (or its lease expires); the next claim resumes only the missing segments (ADR-014) |
| a model OOMs or crashes | that model run fails and is recorded; other models continue; `worker qualify` marks it `oom` or `backend_failure` for this profile |
| CrispASR runtime changed (new worker image with a new CrispASR) | a started model run refuses to resume (fingerprint); create a new sweep |
| driver older than 570 | the worker container does not start ("cuda>=12.8" requirement); `host-setup.sh` catches this first |

## 13. Backups

See [BACKUP.md](BACKUP.md). The `backup` service writes a nightly `pg_dump`,
counts and fingerprints, and the config, to `/srv/aerochorus/backups`. Copy
them off the host.

## 14. Optional services

- **Model adjudication** (ADR-022, paid): set `AEROCHORUS_ADJUDICATOR=1` and
  `AEROCHORUS_OPENROUTER_API_KEY=…` in `aerochorus.env`, then run `$D up`.
  It works only on batches confirmed in the UI under a cost cap.
- **The 5080 PC as an extra worker**: set `AEROCHORUS_API_BIND=<host LAN
  IP>` in `aerochorus.env` and run `$D up`. On Windows, set `api_url =
  "http://<linux-host>:8000"` in the worker config. The PC keeps its own
  hardware profile (`windows_blackwell_16gb`) and the `jesseepc` artifact
  store. The API has no authentication, so do this only on a trusted LAN.

## Fallback: native install

If the NVIDIA Container Toolkit cannot be used on the host, the earlier
native kit (a repository checkout, uv, the pinned binary, systemd) is in
`deploy/linux/native/`. It needs the full toolchain on the host.
