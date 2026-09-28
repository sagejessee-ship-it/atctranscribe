"""Model qualification on a hardware profile (ADR-021).

For each candidate model, on THIS machine: start CrispASR with the model
(one resident at a time), transcribe a small representative set of real
corpus segments, measure load time, VRAM (before / loaded / peak), host RAM,
CUDA-vs-CPU placement, inference time, real-time factor, non-empty rate,
errors, crashes and quality flags, then classify and record the outcome for
(model, hardware_profile).

A model is qualified only if it loads, transcribes representative audio,
returns usable output, and does so without unacceptable failures or speed.
Loading alone is never enough. Results are metrics only: qualification does
not write transcription results (use a sweep for that).
"""

from __future__ import annotations

import hashlib
import logging
import posixpath
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from aerochorus.atc.quality import compute_flags
from aerochorus.contracts import SegmentRead
from aerochorus.sweep_contracts import (
    ModelRead,
    PlatformQualificationWrite,
    PlatformState,
)
from aerochorus.worker.client import ApiClient
from aerochorus.worker.config import WorkerConfig
from aerochorus.worker.crispasr import (
    CrispAsrError,
    Launcher,
    classify_log,
    effective_memory,
    interpret,
    make_launcher,
)
from aerochorus.worker.fs import ReadOnlyCorpusReader
from aerochorus.worker.hardware import gpu_memory_used_mb, resolve_profile
from aerochorus.worker.model_store import ModelArtifactError, ModelStore

log = logging.getLogger(__name__)

# VRAM growth after load that means the weights went to the GPU.
CUDA_VRAM_DELTA_MB = 150


@dataclass(frozen=True)
class Thresholds:
    # inference time / audio time; above this the model cannot keep up with the archive.
    max_rtf: float = 1.0
    # fraction of non-error requests that returned words.
    min_non_empty_rate: float = 0.3
    max_error_rate: float = 0.2


@dataclass
class Measurement:
    loaded: bool = False
    crashed: bool = False
    load_seconds: float | None = None
    requests: int = 0
    errors: int = 0
    non_empty: int = 0
    audio_ms: int = 0
    inference_ms: int = 0
    word_timestamps: int = 0
    token_confidence: int = 0
    flags: Counter = field(default_factory=Counter)
    error_kinds: Counter = field(default_factory=Counter)
    vram_before_mb: int | None = None
    vram_loaded_mb: int | None = None
    vram_peak_mb: int | None = None
    host_ram_peak_mb: int | None = None
    device: str = "unknown"
    log_flags: dict[str, bool] = field(default_factory=dict)
    failure: str | None = None
    samples: list[dict[str, Any]] = field(default_factory=list)

    @property
    def rtf(self) -> float | None:
        return round(self.inference_ms / self.audio_ms, 4) if self.audio_ms else None

    @property
    def non_empty_rate(self) -> float | None:
        answered = self.requests - self.errors
        return round(self.non_empty / answered, 4) if answered > 0 else None


def classify(m: Measurement, memory_strategy: str, t: Thresholds) -> tuple[PlatformState, str]:
    """Qualification state + a one-line reason. Loading alone never qualifies a model."""
    flags = m.log_flags or {}
    if not m.loaded or m.crashed:
        if flags.get("oom"):
            return PlatformState.OOM, m.failure or "out of memory"
        if flags.get("arch_unsupported"):
            return PlatformState.UNSUPPORTED_ON_PLATFORM, m.failure or "no kernel for this GPU"
        return PlatformState.BACKEND_FAILURE, m.failure or "failed to load or crashed"
    if m.requests == 0:
        return PlatformState.BACKEND_FAILURE, "no segments were transcribed"
    if m.errors / m.requests > t.max_error_rate:
        if flags.get("oom"):
            return PlatformState.OOM, f"{m.errors}/{m.requests} requests failed (out of memory)"
        return PlatformState.BACKEND_FAILURE, f"{m.errors}/{m.requests} requests failed"
    rate = m.non_empty_rate
    if rate is None or rate < t.min_non_empty_rate:
        return PlatformState.BACKEND_FAILURE, f"unusable output: non-empty rate {rate}"
    if m.rtf is not None and m.rtf > t.max_rtf:
        return PlatformState.TOO_SLOW, f"RTF {m.rtf} > {t.max_rtf}"
    if m.device == "cpu":
        return PlatformState.QUALIFIED_CPU_ONLY, f"CPU path, RTF {m.rtf}"
    if memory_strategy != "default":
        return PlatformState.QUALIFIED_WITH_OFFLOAD, f"{memory_strategy}, RTF {m.rtf}"
    return PlatformState.QUALIFIED, f"RTF {m.rtf}"


class _Sampler:
    """Polls GPU memory (and the server's host RSS on Linux) while a model is resident."""

    def __init__(self, pid: int | None) -> None:
        self.pid = pid
        self.vram_peak: int | None = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self.stop.wait(0.5):
            used = gpu_memory_used_mb()
            if used is not None:
                self.vram_peak = max(self.vram_peak or 0, used)

    def host_peak_mb(self) -> int | None:
        if not self.pid:
            return None
        try:  # VmHWM = peak resident set size (Linux only)
            for line in Path(f"/proc/{self.pid}/status").read_text().splitlines():
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) // 1024
        except (OSError, ValueError):
            return None
        return None

    def __enter__(self) -> _Sampler:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop.set()
        self.thread.join(timeout=2)


def select_segments(client: ApiClient, sample_id: int) -> list[SegmentRead]:
    """Segments of a stored review sample (deterministic: filters + seed)."""
    sample = client._request("GET", f"/api/v1/review/samples/{sample_id}")
    return [
        SegmentRead.model_validate(client._request("GET", f"/api/v1/segments/{sid}"))
        for sid in sample["segment_ids"]
    ]


def create_sample(client: ApiClient, filters: dict[str, Any], n: int, seed: int, name: str) -> int:
    body = {"filters": {"min_models": 0} | filters, "n": n, "seed": seed, "name": name}
    return client._request("POST", "/api/v1/review/samples", json=body)["id"]


def qualify_model(
    model: ModelRead,
    segments: list[SegmentRead],
    *,
    config: WorkerConfig,
    launcher: Launcher,
    store: ModelStore,
    thresholds: Thresholds,
    pull: bool = False,
) -> tuple[PlatformState, str, dict[str, Any]]:
    tc = config.transcription
    overrides = tc.model_runtime.get(model.logical_name)
    memory = effective_memory(tc.crispasr, overrides)
    m = Measurement()
    runtime: dict[str, Any] = {}
    try:
        path = store.pull(model) if pull else store.require(model)
    except ModelArtifactError as exc:
        m.failure = f"artifact: {exc}"
        state, reason = PlatformState.BACKEND_FAILURE, m.failure
        return state, reason, {"memory": memory, "failure": m.failure}

    readers = {key: ReadOnlyCorpusReader(mount.root) for key, mount in config.sources.items()}
    log_path = tc.artifact_root.parent / "logs" / f"qualify-{model.logical_name}.log"
    log_offset = log_path.stat().st_size if log_path.exists() else 0
    server = None
    m.vram_before_mb = gpu_memory_used_mb()
    try:
        rt = launcher.runtime()
        runtime = rt.describe()
        started = time.monotonic()
        server = launcher.start(
            path,
            model.crisp_backend,
            model.request_params.get("language"),
            log_path,
            overrides=overrides,
        )
        with _Sampler(server.describe.get("pid")) as sampler:
            server.wait_ready(tc.crispasr.startup_timeout_seconds)
            m.loaded = True
            m.load_seconds = round(time.monotonic() - started, 2)
            time.sleep(1.0)  # let the allocator settle before reading VRAM
            m.vram_loaded_mb = gpu_memory_used_mb()
            params = {"response_format": "verbose_json"} | model.request_params
            for segment in segments:
                reader = readers.get(segment.source_key)
                if reader is None:
                    continue
                audio = reader.read_bytes(segment.relative_path)
                if segment.sha256 and hashlib.sha256(audio).hexdigest() != segment.sha256:
                    continue  # changed source: not evidence about the model
                m.requests += 1
                try:
                    response = server.transcribe(
                        audio, posixpath.basename(segment.relative_path), params
                    )
                except httpx.HTTPError as exc:
                    m.errors += 1
                    m.error_kinds[type(exc).__name__] += 1
                    if not server.is_alive():
                        m.crashed = True
                        m.failure = f"server died during inference ({type(exc).__name__})"
                        break
                    continue
                body = response.json()
                if response.status_code != 200 or body is None:
                    m.errors += 1
                    m.error_kinds[f"http_{response.status_code}"] += 1
                    continue
                parsed = interpret(body)
                duration = body.get("duration")
                audio_ms = (
                    round(float(duration) * 1000)
                    if isinstance(duration, int | float)
                    else (segment.duration_ms or 0)
                )
                m.audio_ms += audio_ms
                m.inference_ms += response.elapsed_ms
                m.non_empty += bool(parsed.text)
                m.word_timestamps += parsed.has_word_timestamps
                m.token_confidence += parsed.has_token_confidence
                quality = compute_flags(parsed.text, audio_ms=audio_ms, expected_language="en")
                m.flags.update(quality.flags)
                if len(m.samples) < 5:
                    m.samples.append({"segment_id": segment.id, "text": parsed.text[:200]})
            m.vram_peak_mb = sampler.vram_peak
            m.host_ram_peak_mb = sampler.host_peak_mb()
    except CrispAsrError as exc:
        m.failure = str(exc)[:500]
        m.crashed = m.loaded
    finally:
        if server is not None:
            server.stop()

    log_text = ""
    if log_path.exists():
        with open(log_path, "rb") as fh:
            fh.seek(log_offset)
            log_text = fh.read().decode("utf-8", "replace")
    m.log_flags = classify_log(log_text)
    if m.vram_loaded_mb is not None and m.vram_before_mb is not None:
        cuda = m.vram_loaded_mb - m.vram_before_mb >= CUDA_VRAM_DELTA_MB
        m.device = "cuda" if cuda else "cpu"
    else:
        m.device = "cuda" if m.log_flags.get("cuda") else "unknown"
    state, reason = classify(m, memory["strategy"], thresholds)
    answered = max(m.requests - m.errors, 1)
    metrics = {
        "reason": reason,
        "segments": m.requests,
        "errors": m.errors,
        "error_kinds": dict(m.error_kinds),
        "crashed": m.crashed,
        "failure": m.failure,
        "load_seconds": m.load_seconds,
        "vram_before_mb": m.vram_before_mb,
        "vram_loaded_mb": m.vram_loaded_mb,
        "vram_peak_mb": m.vram_peak_mb,
        "vram_model_mb": (
            m.vram_peak_mb - m.vram_before_mb
            if m.vram_peak_mb is not None and m.vram_before_mb is not None
            else None
        ),
        "host_ram_peak_mb": m.host_ram_peak_mb,
        "device": m.device,
        "audio_ms": m.audio_ms,
        "inference_ms": m.inference_ms,
        "rtf": m.rtf,
        "non_empty_rate": m.non_empty_rate,
        "word_timestamps_rate": round(m.word_timestamps / answered, 3),
        "token_confidence_rate": round(m.token_confidence / answered, 3),
        "flag_rates": {k: round(v / answered, 3) for k, v in sorted(m.flags.items())},
        "log_flags": m.log_flags,
        "memory": memory,
        "runtime": runtime,
        "samples": m.samples,
        "thresholds": thresholds.__dict__,
    }
    return state, reason, metrics


def qualify(
    client: ApiClient,
    config: WorkerConfig,
    models: list[ModelRead],
    segments: list[SegmentRead],
    *,
    profile: str | None = None,
    thresholds: Thresholds | None = None,
    launcher: Launcher | None = None,
    pull: bool = False,
    record: bool = True,
) -> list[dict[str, Any]]:
    if config.transcription is None:
        raise CrispAsrError("this worker has no [transcription] configuration")
    profile = resolve_profile(profile or config.hardware_profile)
    thresholds = thresholds or Thresholds()
    launcher = launcher or make_launcher(config.transcription.crispasr)
    store = ModelStore(config.transcription.models_dir)
    report = []
    for model in models:
        log.info("qualifying %s on %s (%s segments)", model.logical_name, profile, len(segments))
        state, reason, metrics = qualify_model(
            model,
            segments,
            config=config,
            launcher=launcher,
            store=store,
            thresholds=thresholds,
            pull=pull,
        )
        log.info("%s on %s: %s (%s)", model.logical_name, profile, state.value, reason)
        if record:
            client.put_platform_qualification(
                model.logical_name,
                profile,
                PlatformQualificationWrite(
                    state=state,
                    metrics=metrics,
                    notes=reason,
                    recorded_by=config.worker_name,
                ),
            )
        report.append({"model": model.logical_name, "profile": profile, "state": state.value}
                      | {k: metrics.get(k) for k in ("reason", "device", "rtf", "load_seconds",
                                                     "vram_model_mb", "vram_peak_mb",
                                                     "host_ram_peak_mb", "non_empty_rate",
                                                     "errors", "segments")})  # fmt: skip
    return report
