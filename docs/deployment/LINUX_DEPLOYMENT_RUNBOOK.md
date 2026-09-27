# Linux GTX 1070 deployment runbook (ADR-021)

The diagram is in [ARCHITECTURE.md](ARCHITECTURE.md), and data migration from
the Windows dev box is in [MIGRATION.md](MIGRATION.md). Record results in
[LINUX_GTX1070_MIGRATION_REPORT.md](LINUX_GTX1070_MIGRATION_REPORT.md).

Everything below runs **on the Linux host**, from a clone of this repository,
as the user who will own the services. The scripts use `sudo` only for
system files.

## 0. Before you start

- Stop the old Frigate stack if it holds the GPU:
  `docker ps | grep -i frigate`, then `docker compose down` in its directory.
  Check that `nvidia-smi` shows no processes.
- NVIDIA driver: Pascal is supported by the 5xx branches. Driver 580 is the
  last branch with Pascal support, so do not upgrade past it. Any driver
  whose `nvidia-smi` reports CUDA ≥ 12.0 works with the pinned CUDA 12
  build.
- Docker Engine and the compose plugin are required. The NVIDIA container
  toolkit is **not** needed: CrispASR runs natively.

```bash
git clone https://github.com/sagejessee-ship-it/atctranscribe ~/atctranscribe
cd ~/atctranscribe
bash deploy/linux/inventory.sh --json /tmp/inventory.json   # read-only: what is this machine?
```

## 1. Mount the collector's archive read-only

```bash
sudo apt install -y cifs-utils
sudo install -d -m 755 /etc/aerochorus /mnt/aerochorus/atc
sudo install -m 600 /dev/null /etc/aerochorus/smb.cred   # username=… / password=… (read-only share user)
sudoedit /etc/aerochorus/smb.cred
# add the line from deploy/linux/fstab.example to /etc/fstab, then:
sudo systemctl daemon-reload && sudo mount /mnt/aerochorus/atc
ls /mnt/aerochorus/atc/2026/09/08 | head
```

The mount is `ro`, `nofail`, `x-systemd.automount`. If the collector is
offline, boot continues. Scans then report `source_unavailable` and change
nothing, and the UI's audio shows "source audio unavailable" (ADR-013,
ADR-016).

## 2. Run setup

```bash
deploy/linux/setup.sh            # all phases; idempotent (safe to re-run)
deploy/linux/setup.sh --phase smoke
```

| phase | does |
| --- | --- |
| `inventory` | creates `/srv/aerochorus/*`, records OS/CPU/RAM/storage/GPU/driver in `logs/inventory-*.json` |
| `mount` | checks the mount is present and `ro`; a write probe must be refused; reads one real segment |
| `driver` | `nvidia-smi`, the GPU, compute capability, and why CUDA 12 is required |
| `python` | installs `uv` if missing; `uv sync --frozen` |
| `crispasr` | `install-crispasr.sh`: pinned v0.8.37 CUDA 12 tarball, SHA-256 verified, CUDA 12 runtime wheels if the host has none, `--diagnostics` (fails on "no kernel image"), then a source-build fallback with `-DCMAKE_CUDA_ARCHITECTURES=61`; writes `crispasr/current/INSTALLED.json` |
| `config` | `/etc/aerochorus/worker.toml` (profile `linux_pascal_8gb`, mount, native binary, CUDA 12 `LD_LIBRARY_PATH`), `/etc/aerochorus/aerochorus.env`, repository `.env` |
| `services` | Compose: Postgres on loopback, migrations, API on loopback; `models sync`; registers `home_atc_archive` if new |
| `ui` | builds `ui/dist` in a throwaway `node:24` container |
| `systemd` | installs and enables `aerochorus-edge`, `aerochorus-worker`, `aerochorus-backup.timer` |
| `smoke` | `worker qualify --suite smoke-linux1070` (Whisper on CUDA + Parakeet) on 5 real segments, then a tiny 5-segment sweep processed by the worker service, checking that 10 results were persisted |
| `report` | service status and the LAN URL |

The pinned CUDA 12 release was checked (2026-09-27, in a GPU container) to
contain `cuda archs: 60-real,61-real,70-real,75-real,86-real,89-real,120-real`.
Native Pascal kernels are included, so the source build should not be needed.

## 3. Open the UI from the Mac

`http://<linux-host-ip>:8080/review`. `setup.sh` prints the address. If mDNS
(avahi) is running, `http://<hostname>.local:8080` also works. There is no
authentication: keep it on the LAN and never port-forward.

## 4. Services and logs

```bash
systemctl status aerochorus-edge aerochorus-worker
journalctl -u aerochorus-worker -f
docker compose -f docker-compose.yml -f deploy/linux/compose.linux.yml ps
curl -s http://127.0.0.1:8000/health
uv run aerochorus worker inventory          # detected profile, GPU, CUDA, CrispASR identity
```

Reboot test (acceptance): `sudo reboot`, then check that the UI answers,
`systemctl is-active aerochorus-edge aerochorus-worker docker` reports
active, and `/health` returns `ok`.

## 5. Memory strategy on 8 GB

Defaults: quantized weights (q4_k/q8_0 in the catalog), one model resident,
no offload. If a model OOMs or runs too close to 8 GB, give **only that
model** an override in `/etc/aerochorus/worker.toml`, escalating in this
order:

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

The strategy and its env are recorded with every run and every
qualification, and they are part of the runtime fingerprint. Re-qualify
after changing them.

## 6. Qualification (before any production sweep)

```bash
# roster attempt: every candidate, 20 real segments (deterministic sample), pulls models as needed
uv run aerochorus worker qualify --suite qualify-linux1070 --n 20 --seed 1 --pull \
    --utc-from 2026-09-08T12:00:00Z --utc-to 2026-09-09T00:00:00Z
uv run aerochorus models qualifications --profile linux_pascal_8gb
```

The "one-hour qualification corpus" is about 400 BWI segments, since
segments average about 9 s:

```bash
uv run aerochorus worker qualify --suite core-atc-linux1070 --n 400 --seed 2 \
    --utc-from 2026-09-08T00:00:00Z --utc-to 2026-09-09T00:00:00Z
```

States: `qualified`, `qualified_cpu_only`, `qualified_with_offload`,
`too_slow` (RTF > 1.0 by default, `--max-rtf`), `oom`, `backend_failure`
(including "loads but returns nothing usable"), `unsupported_on_platform`,
`not_relevant`, `experimental`. A worker on this profile never claims a
model recorded as `oom`, `backend_failure`, `unsupported_on_platform` or
`not_relevant`, and such a model never blocks the rest of a sweep.

Then decide, explicitly:

```bash
uv run aerochorus models set <name> --eligible            # may full sweeps use it
# ensemble voting is catalog-controlled: set ensemble_eligible = true in config/models.toml
# for a qualified model you trust, then `aerochorus models sync`
# rewrite [suites.full-qualified-linux1070] in config/models.toml from the report, then sync
```

## 7. Production backlog: only on explicit action

1. Validate DB state: `/health`, `aerochorus source summary home_atc_archive`.
2. Validate source paths: `aerochorus worker health`.
3. Run the smoke corpus: `setup.sh --phase smoke`.
4. Run the one-hour qualification (§6) and inspect it.
5. Create the backlog sweep yourself, on the web UI's Transcribe page or
   with `aerochorus sweep create --suite full-qualified-linux1070 …`. The
   worker service processes it; nothing is created automatically.

## 8. Failure behaviour

| event | effect |
| --- | --- |
| collector offline or mount lost | scans and sweeps release work as `source_unavailable`; segments are never marked missing on an unavailable source; the UI audio shows 503 with the reason |
| worker killed or host rebooted mid-sweep | the model run is released or its lease expires; the next claim resumes only the missing segments (ADR-014) |
| a model OOMs or crashes | that model run fails and is recorded; other models continue; `worker qualify` marks it `oom` or `backend_failure` for this profile |
| CrispASR runtime changed | a started model run refuses to resume (fingerprint); create a new sweep |

## 9. Backups

See [BACKUP.md](BACKUP.md): a nightly `aerochorus-backup.timer` writes a
pg_dump plus critical files to `/srv/aerochorus/backups`. Copy them off the
host.
