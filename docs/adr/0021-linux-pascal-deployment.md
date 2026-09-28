# ADR-021: Primary deployment on Linux with a GTX 1070 (8 GB) and 32 GB RAM

Status: Accepted (2026-09-27). Supersedes the macOS parts of ADR-005 and the
"M1 as control plane" plan (docs/plan.md, runbook §10). **Amended by
[ADR-023](0023-container-only-linux-deployment.md):** the worker and review edge
now run as containers (GPU via the NVIDIA Container Toolkit) instead of native
systemd services. Everything else here stands: the host, CUDA 12 on Pascal,
hardware profiles, qualification, and eligibility.

## Context

The plan assumed an Apple Silicon (M1, 16 GB) MacBook as the always-on host
for the control plane and archive worker. That machine is actually an older
Intel Mac with 8 GB RAM, so it is poor for continuous ASR. A Linux desktop with
a GeForce GTX 1070 (8 GB VRAM) and 32 GB RAM is available. It has much more
storage, and it can reach the collector's archive over the LAN.

## Decision

The Linux desktop is the **primary AeroChorus host**. It runs:

- PostgreSQL;
- the API and control plane;
- the review edge, which serves the web UI;
- the worker, CrispASR and the model cache;
- artifacts, annotations, exports, airport data and the context cache.

Three machines, three roles:

| machine | role |
| --- | --- |
| Alienware collector | SDR capture, segmentation, source-audio storage. Unchanged. AeroChorus reads its archive over a **read-only** network mount. |
| Linux host (GTX 1070) | Everything AeroChorus. PostgreSQL and the API run in Docker Compose. The worker, which launches a pinned CrispASR binary one model at a time, and the review edge are native systemd services. |
| Intel Mac, other PCs | Browsers on the LAN. The RTX 5080 box can still be an extra worker or benchmark worker. |

The design principle stays as it was: **a worker executes a model on a
qualified hardware profile.** Nothing in application code is specific to the
GTX 1070.

- **Hardware profile.** A profile id (e.g. `linux_pascal_8gb`) is derived
  from the detected OS, GPU architecture and VRAM, or set in the worker
  config. It is never a hostname. `aerochorus worker inventory` reports it,
  and the heartbeat carries it.
- **Four separate questions** about a model:
  1. Is it in the catalog? (`config/models.toml`)
  2. Is it qualified on a profile? (`model_platform_qualification`)
  3. May full sweeps use it? (`sweep_eligible`)
  4. May it vote in agreement? (`ensemble_eligible`, new) Research-only
     models are recorded and shown but never counted.

  The CrispASR backend list (43 ASR backends in 0.8.37) is not a production
  suite.
- **Qualification harness.** `aerochorus worker qualify` runs each model on
  a deterministic sample of real segments. It records:
  - load time;
  - VRAM before, loaded and peak;
  - host RAM peak;
  - whether the model ran on CUDA or CPU;
  - RTF, non-empty rate, errors, crashes and quality-flag rates;
  - timestamp and confidence support;
  - the memory strategy and the runtime identity.

  The outcome is one of: `qualified`, `qualified_cpu_only`,
  `qualified_with_offload`, `too_slow`, `oom`, `backend_failure`,
  `unsupported_on_platform`, `not_relevant`, `experimental`. Loading alone
  never qualifies a model. Usable output is required.
- **Blocked models do not stop sweeps.** A worker never claims a model
  recorded as `oom`, `backend_failure`, `unsupported_on_platform` or
  `not_relevant` on its profile. Other workers may still claim it.
- **Memory is explicit.** On 8 GB, escalate only as far as needed, in this
  order:
  1. quantized weights (catalog choice);
  2. KV quantization (`CRISPASR_KV_QUANT[_K/_V]`);
  3. partial layer residency (`CRISPASR_N_GPU_LAYERS`);
  4. KV on the CPU (`CRISPASR_KV_ON_CPU`);
  5. CPU.

  These are set per model in `worker.toml [transcription.model_runtime]`.
  They are recorded with every run and part of the runtime fingerprint when
  set. `GGML_CUDA_ENABLE_UNIFIED_MEMORY` is only ever a manual experiment.
  Nothing is enabled silently.
- **CUDA 12, pinned.** Pascal is compute capability 6.1, and CUDA 13 no
  longer generates code for it. `deploy/linux/crispasr.lock` pins CrispASR
  v0.8.37 `crispasr-linux-x86_64-cuda.tar.gz` (the CUDA 12 build) by SHA-256.
  The CUDA 12 runtime libraries come from NVIDIA's wheels when the host has
  none. If the prebuilt binary has no sm_61 kernels, the installer falls back
  to a source build with `-DCMAKE_CUDA_ARCHITECTURES=61` on a CUDA 12.x
  toolkit. It records version, commit, binary hash, CUDA runtime, driver,
  GPU, compute capability, VRAM and build flags.
- **Native worker under systemd.** The worker process gives direct CUDA and
  VRAM inspection, guaranteed cleanup between models, and no dependency on
  the NVIDIA container runtime. The Docker launcher stays for the Windows
  5080 box. It is not a second permanent deployment path on Linux.
- **LAN web.** The review edge binds to the LAN (default port 8080).
  PostgreSQL stays on loopback. The API is loopback unless remote workers
  need it. External credentials stay server-side. There is no public
  exposure.

## Why

- The actual Mac (Intel, 8 GB RAM) cannot sustain broad continuous ASR.
- Linux has 4× the RAM, far more storage, and CUDA acceleration for the
  backends that benefit.
- Remote audio access already exists (the collector's share).
- Web, API, DB and inference can be colocated.
- The worker/API split (ADR-011) makes this a deployment change, not a
  redesign.

## Constraints

- Pascal (6.1) needs CUDA 12.x only, and there is 8 GB of VRAM.
- Some backends are CPU-only in CrispASR (Parakeet, Granite, FastConformer
  CTC, GLM-ASR, Kyutai, …).
- Large models need quantization or offload, or they are excluded. For
  example, Voxtral 4B F16 is 8.9 GB and does not fit.
- Qualification is empirical and specific to each profile.

## Consequences

- The Mac is a browser client only. Its `caffeinate` helper stays as a
  harmless no-op elsewhere; no production path depends on macOS, Metal or
  `host.docker.internal`.
- Linux owns the database, models and artifacts. Source audio stays remote
  and immutable.
- Suites become hardware-qualified: `smoke-linux1070`, `core-atc-linux1070`,
  `qualify-linux1070`, and `full-qualified-linux1070`, which is rewritten
  from the qualification results.
- The production backlog starts only when someone explicitly creates a
  sweep.
