# Self-hosted adjudicator: run, switch profiles, deploy to H100s (ADR-025)

The adjudicator reads one clip (and its squelch-split transmissions), every ASR
hypothesis, the agreement analysis and the airport/ADS-B context, and returns the
transcript as JSON. It is the paid adjudication of
[ADR-022](../adr/0022-model-adjudication.md) with a self-hosted model behind it.
The decisions are in [ADR-025](../adr/0025-self-hosted-adjudication.md).

```text
 host with the audio (this PC or the Linux host)       GPU host (this PC, or the H100 box)
 aerochorus adjudicate run --profile P  ── HTTP ──►    vLLM  (deploy/vllm/compose.yml, service P)
   claims items from the API, reads audio             serves the profile's model as "local/…"
   read-only, sends WAV + context, records results
```

Everything model-specific lives in one file, `config/adjudicators.toml`:
- the client settings: URL, tokens, temperature, audio inputs, timeout;
- the `vllm serve` flags (`[profiles.<name>.vllm]`).

The runner and the server both read it. Switching models means choosing another
profile.

## Profiles

| profile | model | GPUs | expected VRAM | notes |
| --- | --- | --- | --- | --- |
| `local-small` | mistralai/Voxtral-Mini-3B-2507, bf16 | 1 × 16 GB | ~13 GB (0.80 of the card) | needs the card to itself: stop sweeps first |
| `local-small-fp8` | same, online FP8 weights | 1 × 16 GB | ~8 GB (0.50 of the card) | fits beside a running sweep |
| `h100-large` | Qwen/Qwen3-Omni-30B-A3B-Thinking, bf16 | 2 × H100 80 GB (TP=2) | ~60 GB per GPU at 0.90 | reasoning parser `qwen3`; thinking stripped |
| `h100-large-fp8` | same, online FP8 weights | 1 × H100 80 GB | ~70 GB at 0.90 | one GPU; small accuracy cost |
| `h100-voxtral` | mistralai/Voxtral-Small-24B-2507, bf16 | 1 × H100 80 GB | ~65 GB at 0.90 | no system role (instructions in the user turn) |

"Expected VRAM" is what vLLM reserves: weights plus the KV cache it fills up to
`--gpu-memory-utilization`. The weights alone are smaller (Voxtral Mini ≈ 9.5 GB
bf16, Voxtral Small ≈ 55 GB, Qwen3-Omni 30B-A3B ≈ 62 GB bf16).

Each profile sets:
- `model`: the id results are recorded under, and the server's `--served-model-name`;
- `url`: the server;
- `system_prompt`: `user` for models without a system role;
- `max_tokens`, `temperature`, `extra_body`;
- `max_audio_inputs`: the whole clip plus up to N−1 transmissions;
- `structured_output`: JSON-schema guided decoding;
- `parse_retries`, `timeout_s`, `concurrency`.

Verified against vLLM **v0.31.0** (October 2026). The image tag is pinned in
`deploy/vllm/compose.yml` (`VLLM_VERSION`).

The official `vllm/vllm-openai` image leaves out vLLM's audio extra, so Voxtral
fails at start with "`soundfile` is not installed".
- `deploy/vllm/Dockerfile` adds exactly `vllm[audio]` at the same version, and
  nothing else.
- The first `docker compose … up` builds that image as `aerochorus-vllm:<version>`.
  This needs network access on the GPU host.

### Selecting and overriding

| setting | effect |
| --- | --- |
| `--profile P` or `AEROCHORUS_ADJUDICATOR_PROFILE=P` | which profile the runner uses |
| `AEROCHORUS_ADJUDICATOR_URL` | the server, e.g. `http://h100-box:8100/v1` |
| `AEROCHORUS_ADJUDICATOR_API_KEY` | bearer key; the vLLM container requires the same one |
| `AEROCHORUS_ADJUDICATOR_MODEL`, `_MAX_TOKENS`, `_TEMPERATURE`, `_TIMEOUT_S`, `_MAX_AUDIO_INPUTS`, `_STRIP_THINKING` | per-host overrides of the profile |
| `AEROCHORUS_ADJUDICATOR_PROFILES` | another profiles file |

The server side reads `deploy/vllm/.env` (copy `.env.example`):

| setting | effect |
| --- | --- |
| `VLLM_GPUS` | CUDA indexes, e.g. `0,1` for `h100-large` |
| `VLLM_BIND`, `VLLM_PORT` | where it listens |
| `HF_TOKEN` | Hugging Face token |
| `VLLM_CACHE` | weights cache: a host path or the named volume |
| `VLLM_VERSION` | image tag |
| `VLLM_EXTRA_ARGS` | one-off flags |

## Run it on this PC (RTX 5080, Docker Desktop)

Docker Desktop's WSL2 backend passes the GPU to containers, as it does for CrispASR.

```powershell
# 1. The model server (first start downloads ~9.4 GB of weights into the vllm-cache volume)
docker compose -f deploy/vllm/compose.yml --profile local-small-fp8 up -d
docker compose -f deploy/vllm/compose.yml logs -f      # wait for "Application startup complete"
curl http://127.0.0.1:8100/v1/models                   # -> local/voxtral-mini-3b-2507-fp8

# 2. Queue segments for it (free; no paid models here)
uv run aerochorus adjudicate submit --profile local-small-fp8 --segments 3235,443882,660
uv run aerochorus adjudicate submit --profile local-small-fp8 --sample 12        # a stored sample
uv run aerochorus adjudicate submit --profile local-small-fp8 --source home_atc_archive --n 200

# 3. Run them (resumable: stop any time; finished items are never redone)
uv run aerochorus adjudicate run --profile local-small-fp8
uv run aerochorus adjudicate show <batch>

# 4. Gate: accept what >= 2 ASR families corroborate, list the rest for a paid model
uv run aerochorus adjudicate gate <batch>                 # writes adjudication-escalations-<batch>.csv
uv run aerochorus adjudicate gate <batch> --accept        # ...and stores the accepted as silver
```

Results appear in the review inspector next to the hypotheses, as paid ones do.
Use `local-small` (bf16) only when no sweep is using the GPU.

Without Docker (e.g. WSL2 Ubuntu with the CUDA driver): `uv pip install
"vllm[audio]==0.31.0"`, then run the command `serve.py` prints:
`AEROCHORUS_ADJUDICATOR_PROFILES=config/adjudicators.toml python
deploy/vllm/serve.py local-small --dry-run`.

## Evaluate a profile against gold

The ATCO2 benchmark (877 clips, gold in the `reference` schema, already
transcribed by 7 ASR models) is the labelled set:

```powershell
uv run aerochorus adjudicate eval --profile local-small-fp8 --source atco2_fixed --n 100 --seed 1
uv run aerochorus eval adjudication --batch 3 --batch 7     # profiles side by side
```

For each batch the report shows:
- token error rate, as produced and with canonical numbers;
- exact match;
- callsign recall;
- calibration bins with ECE and Brier;
- the gate's accept and escalate rates, with the error rate on each side;
- every ASR model and the ensemble representative on the same clips.

Compare profiles on the **same** segments: use the same `--source`, `--n` and
`--seed`. Results so far are under [Measured](#measured).

## Deploy on the H100 Docker host

Prerequisites:
- NVIDIA driver ≥ 580 (the vLLM v0.31 image is CUDA 13.0);
- Docker Engine with the Compose plugin;
- NVIDIA Container Toolkit;
- about 150 GB of disk for the image and weights.

```bash
git clone <repo> aerochorus && cd aerochorus          # or copy deploy/vllm + config/adjudicators.toml
cp deploy/vllm/.env.example deploy/vllm/.env
# edit deploy/vllm/.env:
#   VLLM_GPUS=0,1                      (h100-large uses tensor parallel 2)
#   VLLM_BIND=<this host's LAN address> VLLM_PORT=8100
#   AEROCHORUS_ADJUDICATOR_API_KEY=<a long random string>   (required off-loopback)
#   HF_TOKEN=<token>                   VLLM_CACHE=/srv/models/hf
docker compose -f deploy/vllm/compose.yml --profile h100-large up -d
```

On the host with the audio, point the runner at it. On this PC:

```powershell
$env:AEROCHORUS_ADJUDICATOR_URL = "http://<h100-host>:8100/v1"
$env:AEROCHORUS_ADJUDICATOR_API_KEY = "<the same key>"
uv run aerochorus adjudicate run --profile h100-large --follow
```

On the Linux host, set these in `/srv/aerochorus/config/aerochorus.env`, then
`deploy.sh up`:
- `AEROCHORUS_ADJUDICATOR=1`
- `AEROCHORUS_ADJUDICATOR_PROFILE=h100-large`
- `AEROCHORUS_ADJUDICATOR_URL`
- `AEROCHORUS_ADJUDICATOR_API_KEY`

Its `adjudicator` container (worker image, with ffmpeg) then takes only that
profile's batches.

### Swap from local-small to h100-large: exact commands

```bash
# GPU host
docker compose -f deploy/vllm/compose.yml --profile local-small-fp8 down    # if it ran there
VLLM_GPUS=0,1 docker compose -f deploy/vllm/compose.yml --profile h100-large up -d
# audio host
aerochorus adjudicate submit --profile h100-large --segments-file adjudication-escalations-<batch>.csv
aerochorus adjudicate run --profile h100-large
```

No code changes. Results are recorded under `local/qwen3-omni-30b-a3b-thinking`,
next to the small model's, never mixed with them.

### Checklist after a swap

1. **Health.** These must answer, and the second must list the profile's model:

   ```bash
   curl -fsS http://<host>:8100/health
   curl -fsS -H "Authorization: Bearer $KEY" http://<host>:8100/v1/models
   ```

   The runner checks the same thing before claiming anything. It stops with
   "serves X, not Y" on a mismatch.
2. **Five-clip smoke test.** Then read the five transcripts next to their
   hypotheses in the review inspector. None may fail, and latency appears per item.

   ```bash
   aerochorus adjudicate submit --profile h100-large --segments 3235,443882,443810,660,3314
   aerochorus adjudicate run --profile h100-large
   aerochorus adjudicate show <batch>
   ```

3. **Evaluation against the small model on the same segments:**

   ```bash
   aerochorus adjudicate eval --profile h100-large --source atco2_fixed --n 100 --seed 1 \
     --compare <the local-small eval batch>
   ```

   Expect lower token error rate and higher callsign recall than both the small
   model and the best single ASR model. Calibration should be no worse (ECE).
4. **Gate rates.** Run `aerochorus adjudicate gate <batch>` on a corpus batch. The
   accepted side's error rate (from step 3) must stay near zero. That is the
   number the escalation saving depends on.

## Troubleshooting

| symptom | cause / fix |
| --- | --- |
| `` `soundfile` is not installed `` at server start | the stock vLLM image without the audio extra. Use the compose file, which builds `deploy/vllm/Dockerfile`. |
| `cannot reach the model server … is it running?` | server down or wrong URL. Check `docker compose … ps` and `logs`. |
| `serves ['local/a'], not local/b` | the server runs another profile. Start the right one or fix `--profile`. |
| `Free memory on device … less than desired GPU memory utilization` | other processes (a sweep) use the GPU. Use `local-small-fp8`, or pause sweeps. |
| every item `unusable answer` | the model ignores the schema. Check the stored response with `adjudicate show`. Try `structured_output = false`, or another profile. |
| `audio could not be prepared` | ffmpeg could not decode the file. The item fails; the batch continues. |
| no squelch splitting | ffmpeg not found (PATH, `AEROCHORUS_FFMPEG` or `--ffmpeg`). The runner prints a warning and sends the original file. |

## Measured

### `local-small-fp8` on the RTX 5080 (2026-10-12)

The setup:
- vLLM v0.31.0 in Docker Desktop.
- Sweep 12 (fastconformer, glm-asr) was transcribing on the same GPU throughout,
  and was not disturbed.
- The server used 5.3 GiB for weights and 1.4 GiB of KV cache (12.5k tokens), about
  7.4 GB in all.
- The first start took about 5 minutes (9.4 GB download). Later starts take about 2
  minutes.

**8 real BWI clips** (batch 1: 3–14 s; 2 two-family agreements, 3 partial, 3
disagreements):
- 8/8 done, 0 parse retries, latency 2.1 s p50 and 2.4 s p95, $0.
- Squelch splitting found 2–3 transmissions in 7 of the 8 clips.
- The model mostly picks a hypothesis rather than adjudicating: 5 of 8 answers are
  one ASR model's text verbatim, formatting included.
- It ignores the transcript conventions (digits, punctuation, capitals).
- It states 0.90–0.95 confidence whether or not it is right.
- The gate escalated all 8: no two families produced the same words on any of them.

**ATCO2, 100 clips** (`adjudicate eval --source atco2_fixed --n 100 --seed 1`,
batch 2), latency 1.3 s p50:

| | token error rate (canonical numbers) | exact | callsign recall |
| --- | --- | --- | --- |
| local-small-fp8 | **0.430** (0.585 as produced) | 4% | 49% |
| best single ASR (whisper-large-v3-turbo) | 0.462 | | |
| ensemble representative | 0.497 | | |

- **Calibration:** ECE 0.857 and Brier 0.774. The model says about 0.91 and is
  exactly right 4% of the time. Do not use its confidence for anything.
- **Gate (≥ 2 families):** accepts 9%, with token error rate 0.18 on the accepted
  side against 0.45 on the escalated side.
- **Gate (≥ 3 families):** accepts 1 clip, and it is wrong.

The accepted-but-wrong clips are short phrases that several models mishear the
same way ("cool" for "wilco", "go ahead" without its callsign). The rest are
spelling differences against the ATCO2 gold ("Juliet"/"Juliett", "it's"/"it is").

**Verdict.** The small profile proves the plumbing end to end, and it beats the
best single ASR model by about 3 points. It is not good enough to auto-accept
anything, so keep `adjudicate gate` without `--accept` until a larger profile
shows a clean accepted side. Run the same `--seed 1` evaluation on `h100-large`
and `h100-voxtral` to compare.
