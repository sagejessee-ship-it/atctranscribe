# Linux GTX 1070 migration report

**Status: prepared and verified off-host. On-host execution is pending.**

**Deployment is containers only (ADR-023).** The images and a deploy bundle
are built on the Windows PC (`deploy\linux\package.ps1`). On the host,
`sudo bash host-setup.sh` runs once, then `bash deploy.sh up`, then
`import.sh` for the data. See
[LINUX_DEPLOYMENT_RUNBOOK.md](LINUX_DEPLOYMENT_RUNBOOK.md).

The Linux host (`192.168.68.53`, reachable on the LAN) accepts SSH only with a
key or password. No key was provided, and passwords are never typed on your
behalf, so the on-host steps have not run yet.

Fill in the **TBD** fields from `/srv/aerochorus/logs/inventory-*.json`,
`deploy.sh smoke`, and `aerochorus models qualifications --profile
linux_pascal_8gb` after running it.

## 1. Detected hardware (on-host: TBD)

| item | value |
| --- | --- |
| OS / kernel | TBD (`deploy/linux/inventory.sh`) |
| CPU | TBD |
| RAM | expected 32 GB (TBD) |
| GPU | expected GeForce GTX 1070, 8 GB, compute capability 6.1 (TBD) |
| NVIDIA driver / max CUDA | TBD (Pascal supported through the 580 branch) |
| storage (`/`, `/srv`) | TBD |
| profile | expected `linux_pascal_8gb` |

## 2. CrispASR runtime

| item | value |
| --- | --- |
| version / tag / commit | 0.8.37 / v0.8.37 / `d08ec2dd83411a8389745c97c4f2e3955e084280` |
| asset | `crispasr-linux-x86_64-cuda.tar.gz` (CUDA 12; **not** the cuda13 asset) |
| asset SHA-256 (GitHub-published, verified on download) | `616298b500915608c48726eed6e4943f2ce5be368ae823e695142a6aa4cdc4b0` |
| binary SHA-256 | `5f3d8953c72ed2e6267eaaa42d5d5d8d48187d7722f5b95f55a32333b6034a00` (from the verified tarball) |
| build | CUDA toolkit 12.8.93, runtime ABI 12, **cuda archs `60-real,61-real,70-real,75-real,86-real,89-real,120-real,120-virtual`**: native Pascal (sm_61) kernels are included, so no source build is expected |
| CUDA 12 runtime | inside the `aerochorus-worker` image: `nvidia/cuda:12.8.1-base` (cudart) plus cuBLAS/cuBLASLt 12.8 only. The host needs just the driver (≥ 570, ≤ 580 branch) and the NVIDIA Container Toolkit. |
| CUDA 13 required? | **No.** The installer refuses a cuda13 asset, and inventory flags compute capability < 7.5. |

**Verified off-host, containers (2026-09-27):** the `aerochorus-worker` image
was built on the 5080 PC. The binary sha256 inside it matches the lock. With
the GPU passed through (Docker Desktop, WSL2), `aerochorus worker crispasr
check` loaded the **CUDA backend** and transcribed a real 17.6 s BWI tower
segment from 2026-09-26 with Parakeet in 266 ms.

**Verified off-host, native kit (earlier the same day)** in an Ubuntu 24.04
container on the 5080 box with the GPU passed through:

- `install-crispasr.sh` downloaded and SHA-256-verified the pinned tarball;
- it installed the CUDA 12 runtime wheels and set `LD_LIBRARY_PATH`;
- `crispasr --diagnostics` loaded the **CUDA backend** (`libggml-cuda.so`)
  and saw the GPU;
- the CPU-only asset path also verified end to end;
- `inventory.sh` produced a correct report and profile;
- all shell scripts pass `bash -n`.

## 3. Source mount

| item | value |
| --- | --- |
| collector share | `//192.168.68.84/bwi` (unchanged; the collector is not modified) |
| mount | `/mnt/aerochorus/atc`, CIFS `ro,nofail,_netdev,x-systemd.automount` (`deploy/linux/fstab.example`) |
| read-only check | `host-setup.sh` step 6: `ro` in `/proc/mounts`, and a write probe must be refused; `deploy.sh smoke` checks the archive is readable inside the containers (TBD) |
| identity | `(home_atc_archive, relative path)`; the mount path is only in `worker.toml` |

## 4. Storage and services

`/srv/aerochorus/{config,deploy,postgres,models,artifacts,artifacts-jesseepc,exports,cache,corpora,logs,backups}`

All services belong to one Compose project (`deploy/linux/compose.yml`) with
`restart: unless-stopped`. They run as the host operator, except
PostgreSQL.

| service | image | bind |
| --- | --- | --- |
| `postgres` (PostgreSQL 17, data in `/srv/aerochorus/postgres`) | `postgres:17` | 127.0.0.1:5432 |
| `api` | `aerochorus-app` | 127.0.0.1:8000 (LAN only if remote workers need it) |
| `edge` (review web UI) | `aerochorus-app` | 0.0.0.0:8080 → **LAN URL `http://<host-ip>:8080/review`** (TBD) |
| `worker` + CrispASR (GPU; one model resident at a time) | `aerochorus-worker` | — |
| `backup` (nightly pg_dump + counts + config) | `postgres:17` | — |
| `adjudicator` (optional, paid): **off** unless `AEROCHORUS_ADJUDICATOR=1`. It holds `AEROCHORUS_OPENROUTER_API_KEY` from `/srv/aerochorus/config/aerochorus.env` and works only on batches confirmed in the UI under a cost cap ([ADR-022](../adr/0022-model-adjudication.md)). | `aerochorus-app` | outbound HTTPS only |

## 5. Data migration

The Windows dev box holds 448,899 segments, 27,823 results, 877 gold
references, 1 annotation and 6 sweeps. The export, restore and verification
scripts exist, and the round trip was **verified identical** (13 tables,
counts and content fingerprints). See [MIGRATION.md](MIGRATION.md). The
on-host import is TBD.

The schema is now at migration **0010**:

- 0009: runway-end coordinates and FAA airspace for the map;
- 0010: adjudication batches and items, and the `model_adjudicated` text
  origin.

The import restores a dump from the Windows box at its revision, and
`docker compose up` then applies anything newer. Re-export on migration day,
because the test bundle's `counts.tsv` was overwritten.

After the import, recreate the map geometry if the dump predates 0009:

```bash
uv run aerochorus airport bootstrap KBWI --timezone America/New_York --station BWI
uv run aerochorus airport airspace KBWI
```

OpenSky: the account logs in, but Trino answers "Access Denied" until
OpenSky grants historical access. The REST fallback needs an API client
(`AEROCHORUS_OPENSKY_CLIENT_ID/SECRET`). See
[OPENSKY_PROVIDER.md](../context/OPENSKY_PROVIDER.md).

## 6. Model qualification

The harness (`aerochorus worker qualify`) is proven end to end with real
CrispASR on real BWI audio, on the 5080 box (reference profile, not the
target):

| profile | model | state | device | RTF | load s | model VRAM | non-empty |
| --- | --- | --- | --- | --- | --- | --- | --- |
| windows_blackwell_16gb | parakeet-tdt-0.6b-v3-q8_0 | qualified | cuda | 0.016 | 7.2 | 1,326 MB | 0.90 |
| windows_blackwell_16gb | whisper-large-v3-turbo-q8_0 | qualified | cuda | 0.028 | 7.1 | 1,484 MB | 1.00 |

**linux_pascal_8gb** is TBD: run §6 of the runbook. The initial diverse ATC
roster to attempt is `qualify-linux1070` (17 candidates across 14
architecture families):

| candidate | family | backend | artifact | expected on 8 GB |
| --- | --- | --- | --- | --- |
| parakeet-tdt-0.6b-v3 (q8_0, q4_k) | parakeet | parakeet | 674 / 418 MB | CPU path per CrispASR docs; fits |
| canary-1b-v2-q8_0 | canary | canary | 1.0 GB | CUDA; fits |
| whisper-large-v3-turbo-q8_0 | whisper | whisper | 0.9 GB | CUDA; fits |
| qwen3-asr-0.6b-q8_0 / 1.7b-q4_k | qwen3-asr | qwen3 / qwen3-1.7b | 1.0 / 1.5 GB | CUDA; fits |
| granite-speech-4.1-2b-q4_k | granite-speech | granite-4.1 | 2.9 GB | CPU path per docs |
| voxtral-mini-3b-2507-q4_k | voxtral | voxtral | 2.7 GB | CUDA; likely fits |
| voxtral-mini-4b-realtime-q4_k | voxtral | voxtral4b | 2.5 GB | CUDA; F16 (8.9 GB) excluded |
| canary-qwen-2.5b-q4_k | canary-qwen | canary-qwen | 3.7 GB | CUDA; the first likely to need KV quantization |
| cohere-transcribe-q4_k | cohere-transcribe | cohere | 1.5 GB | CUDA |
| kyutai-stt-1b-q8_0 | kyutai-stt | kyutai-stt | 1.2 GB | CPU path per docs |
| glm-asr-nano-q4_k | glm-asr | glm-asr | 1.3 GB | CPU path per docs |
| fastconformer-ctc-large-q8_0 | fastconformer-ctc | fastconformer-ctc | 0.1 GB | CPU path per docs |
| wav2vec2-xlsr-53-en-q8_0 | wav2vec2 | wav2vec2 | 0.4 GB | CUDA |
| gemma4-e2b-it-q4_k | gemma4-audio | gemma4-e2b | 2.8 GB | CUDA |
| omniasr-ctc-1b-v2-q8_0 | omniasr | omniasr | 1.1 GB | some modes CPU |

Not attempted, and why:

- Moonshine and MiMo-ASR need companion tokenizer files the registry does
  not pin yet;
- FireRedASR is Mandarin-first;
- TTS and music backends are not ASR.

## 7. Recommended suites (provisional until the 1070 qualification)

- `core-atc-linux1070`: parakeet-tdt-0.6b-v3-q8_0, canary-1b-v2-q8_0,
  whisper-large-v3-turbo-q8_0, qwen3-asr-0.6b-q8_0. These are four
  independent families that already agree well on BWI audio.
- `full-qualified-linux1070`: provisionally the same four. Rewrite it after
  qualification, from models in `qualified*` states whose output was
  inspected. Candidates for promotion are qwen3-asr-1.7b (the best single
  model on the ATCO2 re-score), canary-qwen, cohere and kyutai. Keep new
  families `ensemble_eligible = false` until their errors have been
  reviewed.

## 8. Acceptance criteria

| # | criterion | state |
| --- | --- | --- |
| 1 | inventory confirms GTX 1070 8 GB + 32 GB RAM | TBD on host (`inventory.sh`) |
| 2 | CrispASR runs CUDA on the GTX 1070 | TBD on host; the pinned build contains sm_61 and its CUDA backend loads (verified in a container) |
| 3 | CUDA 13 not required | **done**: CUDA 12 asset pinned; cuda13 refused |
| 4 | exact CrispASR version and hash recorded | **done** (above; `INSTALLED.json` on host) |
| 5 | one CUDA backend transcribes a real segment | TBD on the 1070 (`worker qualify --suite smoke-linux1070`); **done in the container on the 5080** (Parakeet, CUDA backend, real 09-26 segment) |
| 6 | one CPU-only backend transcribes a real segment | TBD (smoke: Parakeet; `worker qualify` records `device`) |
| 7 | web UI reachable from the Intel Mac | TBD (`http://<host>:8080/review`) |
| 8 | DB/API/web/worker survive reboot | TBD (Compose `restart: unless-stopped`, Docker enabled at boot; reboot test in the runbook §7) |
| 9 | source audio read over the network mount | TBD (`host-setup.sh` step 6, `deploy.sh smoke`) |
| 10 | mount loss does not corrupt or delete state | **done in tests** (`source_unavailable` scans; edge 503; sweeps release work) |
| 11 | source audio cannot be modified through AeroChorus | **done**: ro mount, write probe, architecture tests, read-only reader |
| 12 | killed model resumes correctly | **done in tests** (`test_hard_kill_is_recovered_by_the_same_worker`, `test_interrupted_run_resumes_only_missing_work`) |
| 13 | qualification records memory/RTF/status | **done** (harness verified on real audio; `model_platform_qualification`) |
| 14 | initial diverse ATC roster attempted | TBD on host (`qualify-linux1070`, 17 candidates) |
| 15 | OOM/incompatible models marked, not fatal | **done** (states + claim filter, `test_blocked_models_are_left_for_other_profiles`) |
| 16 | existing tests still pass | **done** (backend 208 + unit, vitest, Playwright 11) |
| 17 | Mac/Metal assumptions superseded | **done** (ADR-021; ADR-005/006, plan, runbook §10, README, example config) |
| 18 | docs show the three-machine topology | **done** ([ARCHITECTURE.md](ARCHITECTURE.md), README) |
| 19 | production backlog only after explicit action | **done** (no automatic sweeps; runbook §7) |

## 9. Remaining risks

- The driver version on the host is unknown. The worker image needs ≥ 570
  (CUDA 12.8), and 580 is the last branch for Pascal. `host-setup.sh
  --install-driver` installs 580.
- Frigate may hold the GPU or ports. `host-setup.sh --stop-frigate` stops it.
- CPU-path backends (Parakeet, Granite, …) may be `too_slow` at RTF > 1 on
  this CPU. The qualification RTF decides. The 5080 box can remain a worker
  for heavy models.
- There is no authentication on the LAN UI. It must never be port-forwarded.
- Backups need an off-host copy to protect against disk loss.
- Adjudication sends selected audio to OpenRouter (Google). It is off until
  `AEROCHORUS_ADJUDICATOR=1`. Each batch is confirmed and capped.
- The deploy bundle is about 2 GB per update, because base layers are copied
  each time.

## 10. To finish on the host

On the Windows PC:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\migrate\export-windows.ps1   # data
powershell -ExecutionPolicy Bypass -File deploy\linux\package.ps1            # images + kit
scp -r <bundle> <export> <user>@192.168.68.53:~/                              # or a USB disk
```

On the host:

```bash
cd ~/aerochorus-deploy-<tag>
sudo bash host-setup.sh --stop-frigate
bash deploy.sh up
/srv/aerochorus/deploy/import.sh ~/<export stamp>
aerochorus worker models pull --suite qualify-linux1070
D=/srv/aerochorus/deploy/deploy.sh; $D compose stop worker
$D compose run --rm worker aerochorus worker qualify --suite qualify-linux1070 --n 20 --seed 1 --pull
$D compose start worker
aerochorus models qualifications --profile linux_pascal_8gb   # paste into §6
```
