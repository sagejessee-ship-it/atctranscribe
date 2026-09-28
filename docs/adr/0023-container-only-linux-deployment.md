# ADR-023: The Linux host runs containers only

Status: Accepted (2026-09-27). Amends [ADR-021](0021-linux-pascal-deployment.md),
which had the worker and review edge as native systemd services.

## Context

ADR-021 put everything AeroChorus on the Linux GTX 1070 desktop. Its kit ran
PostgreSQL and the API in Compose, and everything else natively: a repository
checkout, uv and a Python virtualenv, the pinned CrispASR binary with CUDA 12
runtime wheels, a Node build of the UI, and systemd units.

The operator wants the Linux box to be a **deployment target only**. It should
not be a second development environment with a repository, toolchains or an
editor. Everything is developed and built on the Windows RTX 5080 PC. The GPU
passes into containers through the NVIDIA Container Toolkit. The box already
ran GPU containers for Frigate.

## Decision

- **Two images, one Dockerfile.** Both are built on the dev PC.
  - `aerochorus-app` (target `app`) runs the API, migrations, the review edge
    with the built web UI, and the adjudication runner.
  - `aerochorus-worker` (target `worker`) contains the worker, ffmpeg and the
    pinned CrispASR build. It is based on `nvidia/cuda:12.8.1-base` plus
    cuBLAS 12 only.
  - The CrispASR build is v0.8.37, the CUDA 12 asset. It is downloaded at
    build time and verified against `deploy/linux/crispasr.lock`, and a
    CUDA 13 asset is refused. Its CUDA architectures include `61-real`
    (Pascal) and `120-real`.
- **A checksummed bundle, not a registry.** `deploy/linux/package.ps1` writes
  `aerochorus-deploy-<date>-<commit>/`:
  - the saved images (`docker save`, gzip), including `postgres:17`;
  - the kit: compose file, scripts and config templates;
  - `IMAGES.txt` (tag, commit, image ids) and `SHA256SUMS`.

  The operator copies it over (scp, disk or share). No credentials for an
  image registry are needed, and the images on the host are exactly the ones
  built and tested.
- **The host gets only prerequisites.** `host-setup.sh` is run once, as root,
  and is idempotent. It:
  - takes an inventory;
  - checks the NVIDIA driver (≥ 570 for the CUDA 12.8 runtime; 580 is the
    last branch for Pascal), and installs one only on request;
  - installs Docker Engine with the Compose plugin, and the NVIDIA Container
    Toolkit, from the vendors' apt repositories;
  - proves a container sees the GPU;
  - offers to stop Frigate;
  - mounts the collector share read-only (CIFS, credentials root-only) and
    proves that a write is refused;
  - creates `/srv/aerochorus`.
- **`deploy.sh up` is both install and update.** It:
  1. verifies the bundle checksums;
  2. copies the kit to `/srv/aerochorus/deploy`, keeping the previous one as
     `deploy.prev`;
  3. loads the images;
  4. writes `/srv/aerochorus/config/aerochorus.env` on the first run (with a
     random database password, chmod 600) and `worker.toml`, then only
     updates the image tag;
  5. runs `docker compose up -d`;
  6. smoke-tests: API health, the UI with the archive readable inside the
     container, the GPU inside the worker, the CrispASR identity, and a worker
     heartbeat.

  Rollback means pointing `AEROCHORUS_TAG` at the previous, still-loaded
  images.
- **Services** (`deploy/linux/compose.yml`, restart unless-stopped):

  | service | role | exposure |
  | --- | --- | --- |
  | `postgres` | data in `/srv/aerochorus/postgres` | loopback |
  | `migrate` | one-shot migrations | — |
  | `api` | control plane | loopback |
  | `edge` | web UI | LAN (:8080) |
  | `worker` | GPU reservation; stops with SIGINT so a model run is released | — |
  | `backup` | nightly `pg_dump` in a `postgres:17` container | — |
  | `adjudicator` | optional (profile), the only holder of the OpenRouter key | — |
  | `cli` | one-off commands through the `aerochorus` wrapper | — |

  The archive is mounted read-only into the worker, edge, adjudicator and
  cli (read-only kernel mount plus a `:ro` bind).
- **Config and secrets** live in `/srv/aerochorus/config`, outside the kit
  and the images. They survive updates, and the nightly backup includes
  them.
- The native kit is kept in `deploy/linux/native/` as a fallback only, for a
  host where the NVIDIA Container Toolkit cannot be used.

## Consequences

- Updating the host means building on the PC, copying one folder and
  running `bash deploy.sh up`. The host never compiles anything or runs `npm`
  or `uv`.
- The bundle is large: about 5 GB of images uncompressed, mostly the CUDA
  libraries, and roughly 2 GB compressed. Unchanged base layers are
  deduplicated by `docker load` on the host, but they are still copied each
  time. A registry would avoid that at the cost of credentials. It is not
  used.
- The worker container depends on the NVIDIA Container Toolkit and a driver
  ≥ 570. `host-setup.sh` checks both. An older driver makes the worker fail
  to start with a clear "cuda>=12.8" requirement error, and nothing fails
  silently.
- Qualification (GPU work) runs in the worker service's container, with the
  daemon stopped for the duration, so the two never compete for 8 GB of
  VRAM.

## Verification

On the Windows RTX 5080 PC (Docker Desktop with the WSL2 GPU), 2026-09-27:

- Both images built. The worker image contains CrispASR 0.8.37 (`d08ec2dd`),
  and its binary sha256 matches the lock.
- `aerochorus worker crispasr check` in the worker container, with the GPU
  passed through, loaded the **CUDA backend** (`ggml_cuda_init: found 1 CUDA
  devices … RTX 5080, compute capability 12.0`). It transcribed a real
  17.6 s BWI tower segment from 2026-09-26 in 266 ms with Parakeet.
- `deploy/linux/compose.yml` validates with every profile.
- **The whole path ran as on the host.** `package.ps1` produced a 1.86 GB
  bundle whose checksums verify. `deploy.sh up` ran from the bundle as an
  isolated Compose project with its own data directory and ports, and all
  smoke checks passed: the API was healthy, the UI was up with the archive
  readable inside the container, the worker container saw the GPU, the
  CrispASR identity was reported, and the worker heartbeat arrived. The UI
  was served from the edge container and showed the worker and its GPU.
  Also checked:
  - the `aerochorus` CLI wrapper ran commands in one-off containers;
  - `deploy.sh backup` wrote a dump, counts, config and `SHA256SUMS`;
  - `deploy.sh down` stopped the worker cleanly (SIGINT) in 3 s.

  This found and fixed a first-start race: during Postgres's first
  initialisation, a Unix-socket `pg_isready` passed before the real server
  was up, so the healthcheck now uses TCP.

On-host verification on the GTX 1070 is recorded in
[LINUX_GTX1070_MIGRATION_REPORT.md](../deployment/LINUX_GTX1070_MIGRATION_REPORT.md).
