"""Sweep worker: claim a model run, serve it with one CrispASR process, persist
every result immediately, stop the process, move on (ADR-006, ADR-007).

Resume is the same code path as a fresh start: the API hands out only the
segments that have no result yet for this model run.
"""

from __future__ import annotations

import hashlib
import logging
import os
import posixpath
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from aerochorus.sweep_contracts import (
    ClaimRequest,
    ModelRunClaim,
    ModelRunFinish,
    ModelRunStart,
    ModelRunStatus,
    PendingSegment,
    ResultPost,
    ResultStatus,
    SweepStatus,
)
from aerochorus.worker.artifacts import ArtifactStore
from aerochorus.worker.client import ApiClient, ApiError
from aerochorus.worker.config import WorkerConfig
from aerochorus.worker.crispasr import (
    CrispAsrError,
    Launcher,
    Runtime,
    Server,
    effective_memory,
    interpret,
    make_launcher,
)
from aerochorus.worker.fs import ReadOnlyCorpusReader, SourceUnavailable
from aerochorus.worker.hardware import resolve_profile
from aerochorus.worker.health import collect_health, heartbeat_from_health
from aerochorus.worker.model_store import ModelArtifactError, ModelStore
from aerochorus.worker.power import keep_awake

log = logging.getLogger(__name__)

# Give up on a model run after this many consecutive per-segment failures:
# something is wrong with the server, not with the audio.
MAX_CONSECUTIVE_ERRORS = 5


def _is_within(child: Path, parent: Path) -> bool:
    child_s = os.path.normcase(os.path.abspath(child))
    parent_s = os.path.normcase(os.path.abspath(parent))
    try:
        return os.path.commonpath([child_s, parent_s]) == parent_s
    except ValueError:  # different drives / UNC hosts
        return False


class SmokeFailed(RuntimeError):
    pass


class Released(Exception):
    """The model run was handed back to the queue (pause, cancel, shutdown)."""


@dataclass
class Outcome:
    sweep_id: int
    sweep_model_id: int
    model: str
    status: str
    processed: int
    message: str | None = None


class SweepWorker:
    def __init__(
        self,
        client: ApiClient,
        config: WorkerConfig,
        *,
        launcher: Launcher | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        if config.transcription is None:
            raise CrispAsrError("this worker has no [transcription] configuration")
        self.client = client
        self.config = config
        self.tc = config.transcription
        self.store = ModelStore(self.tc.models_dir)
        self.artifacts = ArtifactStore(self.tc.artifact_root, self.tc.artifact_store)
        self.launcher = launcher or make_launcher(self.tc.crispasr)
        self.hardware_profile = resolve_profile(config.hardware_profile)
        self.stop = stop or threading.Event()
        self.readers = {
            key: ReadOnlyCorpusReader(mount.root) for key, mount in config.sources.items()
        }
        self.log_dir = self.tc.artifact_root.parent / "logs"
        for key, mount in config.sources.items():
            for label, path in (
                ("artifact_root", self.tc.artifact_root),
                ("models_dir", self.tc.models_dir),
                ("logs", self.log_dir),
            ):
                if _is_within(path, mount.root):
                    raise CrispAsrError(
                        f"{label} {path} is inside corpus source {key!r}; AeroChorus never "
                        "writes into source audio"
                    )

    # -- loop ---------------------------------------------------------------------------

    def run_until_idle(self, max_model_runs: int | None = None) -> list[Outcome]:
        health = collect_health(self.config, self.client)
        self.client.heartbeat(heartbeat_from_health(self.config, health))
        outcomes: list[Outcome] = []
        while not self.stop.is_set():
            if max_model_runs is not None and len(outcomes) >= max_model_runs:
                break
            outcome = self.process_next()
            if outcome is None:
                break
            outcomes.append(outcome)
        return outcomes

    def process_next(self) -> Outcome | None:
        claim = self.client.claim_model_run(
            ClaimRequest(
                worker_name=self.config.worker_name,
                lease_seconds=self.tc.lease_seconds,
                source_keys=sorted(self.config.sources),
                hardware_profile=self.hardware_profile,
            )
        )
        if claim is None:
            return None
        log.info(
            "claimed sweep %s model %s (attempt %s, %s/%s already recorded)",
            claim.sweep_id,
            claim.model.logical_name,
            claim.attempt,
            claim.results_recorded,
            claim.segments_total,
        )
        with keep_awake(f"sweep {claim.sweep_id} {claim.model.logical_name}"):
            return self._process(claim)

    # -- one model run --------------------------------------------------------------------

    def _finish(self, claim: ModelRunClaim, status: ModelRunStatus, message: str | None) -> None:
        self.client.finish_model_run(
            claim.sweep_model_id,
            self.config.worker_name,
            ModelRunFinish(status=status, error_message=message),
        )

    def _process(self, claim: ModelRunClaim) -> Outcome:
        model = claim.model
        outcome = Outcome(claim.sweep_id, claim.sweep_model_id, model.logical_name, "", 0)
        server: Server | None = None
        try:
            model_path = self._ensure_model(claim)
            runtime = self.launcher.runtime()
            overrides = self.tc.model_runtime.get(model.logical_name)
            memory = effective_memory(self.tc.crispasr, overrides)
            fingerprint = runtime.fingerprint(model.model_sha256 or "", model.crisp_backend, memory)
            if (
                claim.runtime_fingerprint
                and claim.runtime_fingerprint != fingerprint
                and claim.results_recorded
            ):
                raise CrispAsrError(
                    f"this worker's CrispASR runtime ({runtime.version}, {runtime.artifact[:19]}) "
                    "differs from the one the model run started with; results would not be "
                    "comparable. Create a new sweep to use this runtime."
                )
            log_path = self.log_dir / f"sweep{claim.sweep_id}-{model.logical_name}.log"
            started = time.monotonic()
            server = self.launcher.start(
                model_path,
                model.crisp_backend,
                claim.request_params.get("language"),
                log_path,
                overrides=overrides,
            )
            health = server.wait_ready(self.tc.crispasr.startup_timeout_seconds)
            self._verify_identity(claim, health, server)
            load_s = round(time.monotonic() - started, 1)
            log.info("%s loaded in %ss (%s)", model.logical_name, load_s, health)
            self.client.start_model_run(
                claim.sweep_model_id,
                ModelRunStart(
                    worker_name=self.config.worker_name,
                    runtime=runtime.describe()
                    | {
                        "load_seconds": load_s,
                        "server": server.describe,
                        "health": health,
                        "memory": memory,
                        "hardware_profile": self.hardware_profile,
                    },
                    runtime_fingerprint=fingerprint,
                    lease_seconds=self.tc.lease_seconds,
                ),
            )
            self._serve(claim, server, runtime, outcome)
            self._finish(claim, ModelRunStatus.COMPLETED, None)
            outcome.status = "completed"
        except Released as exc:
            outcome.status, outcome.message = "released", str(exc)
        except (CrispAsrError, SmokeFailed, ModelArtifactError) as exc:
            log.error("model run %s failed: %s", claim.sweep_model_id, exc)
            self._finish(claim, ModelRunStatus.FAILED, str(exc)[:2000])
            outcome.status, outcome.message = "failed", str(exc)
        except ApiError as exc:
            # e.g. the control plane refused to resume with a different runtime.
            log.error("model run %s refused: %s", claim.sweep_model_id, exc)
            self._finish(claim, ModelRunStatus.FAILED, str(exc)[:2000])
            outcome.status, outcome.message = "failed", str(exc)
        except SourceUnavailable as exc:
            # Network trouble is not a model failure: hand the run back untouched.
            self.client.release_model_run(
                claim.sweep_model_id, self.config.worker_name, f"source unavailable: {exc}"
            )
            outcome.status, outcome.message = "released", f"source unavailable: {exc}"
        except BaseException:
            # Ctrl-C or a crash: give the run back so it resumes cleanly later.
            try:
                self.client.release_model_run(
                    claim.sweep_model_id, self.config.worker_name, "worker interrupted"
                )
            except Exception:  # noqa: BLE001 - lease expiry covers an unreachable API
                log.warning("could not release model run %s", claim.sweep_model_id)
            raise
        finally:
            if server is not None:
                server.stop()
        log.info(
            "model run %s (%s): %s after %s segments",
            claim.sweep_model_id,
            model.logical_name,
            outcome.status,
            outcome.processed,
        )
        return outcome

    def _ensure_model(self, claim: ModelRunClaim) -> Path:
        model = claim.model
        check = self.store.verify(model)
        if not check.present and self.tc.auto_pull_models:
            log.info("pulling %s", model.logical_name)
            return self.store.pull(model)
        return self.store.require(model)

    def _verify_identity(
        self, claim: ModelRunClaim, health: dict[str, Any], server: Server
    ) -> None:
        backend = health.get("backend")
        if backend and backend != claim.model.crisp_backend:
            raise CrispAsrError(
                f"server reports backend {backend!r}, expected {claim.model.crisp_backend!r}"
            )
        loaded = [posixpath.basename(m.replace("\\", "/")) for m in server.loaded_models()]
        if loaded and claim.model.model_filename not in loaded:
            raise CrispAsrError(f"server loaded {loaded}, expected {claim.model.model_filename}")

    def _serve(
        self, claim: ModelRunClaim, server: Server, runtime: Runtime, outcome: Outcome
    ) -> None:
        consecutive_errors = 0
        smoke_done = False
        while True:
            batch = self.client.pending_segments(claim.sweep_model_id, self.tc.pending_batch)
            if batch.sweep_status != SweepStatus.RUNNING or (
                batch.model_status != ModelRunStatus.RUNNING
            ):
                self.client.release_model_run(
                    claim.sweep_model_id,
                    self.config.worker_name,
                    f"sweep is {batch.sweep_status}",
                )
                raise Released(f"sweep is {batch.sweep_status}")
            if not batch.segments:
                return
            for segment in batch.segments:
                if self.stop.is_set():
                    self.client.release_model_run(
                        claim.sweep_model_id, self.config.worker_name, "worker stopping"
                    )
                    raise Released("worker stopping")
                result = self._transcribe(claim, server, runtime, segment, smoke=not smoke_done)
                smoke_done = smoke_done or result.status != ResultStatus.ERROR
                self.client.post_result(claim.sweep_model_id, self.config.worker_name, result)
                outcome.processed += 1
                if result.status == ResultStatus.ERROR:
                    consecutive_errors += 1
                    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        raise CrispAsrError(
                            f"{consecutive_errors} consecutive errors; last: "
                            f"{result.error_type}: {result.error_message}"
                        )
                    if not server.is_alive():
                        raise CrispAsrError("CrispASR server exited")
                else:
                    consecutive_errors = 0

    # -- one segment ------------------------------------------------------------------------

    def _transcribe(
        self,
        claim: ModelRunClaim,
        server: Server,
        runtime: Runtime,
        segment: PendingSegment,
        *,
        smoke: bool,
    ) -> ResultPost:
        result_id = uuid.uuid4()

        def error(kind: str, message: str, *, server_side: bool = True, **extra: Any) -> ResultPost:
            # The first request that reaches the server is the smoke test: if the
            # server cannot answer it, the model run fails instead of recording errors.
            if smoke and server_side:
                raise SmokeFailed(f"smoke segment {segment.relative_path}: {kind}: {message}")
            return ResultPost(
                id=result_id,
                segment_id=segment.segment_id,
                status=ResultStatus.ERROR,
                error_type=kind,
                error_message=message[:2000],
                lease_seconds=self.tc.lease_seconds,
                **extra,
            )

        reader = self.readers.get(segment.source_key)
        if reader is None:
            return error(
                "source_not_mapped",
                f"no root configured for {segment.source_key}",
                server_side=False,
            )
        try:
            audio = reader.read_bytes(segment.relative_path)
        except OSError as exc:
            reader.require_available()
            return error("source_read_error", f"{type(exc).__name__}: {exc}", server_side=False)
        audio_sha256 = hashlib.sha256(audio).hexdigest()
        if segment.sha256 and audio_sha256 != segment.sha256:
            return error(
                "source_changed",
                f"audio sha256 {audio_sha256} differs from the indexed {segment.sha256}",
                server_side=False,
                audio_sha256=audio_sha256,
            )

        params = claim.request_params
        try:
            response = server.transcribe(audio, posixpath.basename(segment.relative_path), params)
        except httpx.TimeoutException as exc:
            return error("timeout", str(exc) or "request timed out", audio_sha256=audio_sha256)
        except httpx.TransportError as exc:
            return error(
                "server_unavailable", f"{type(exc).__name__}: {exc}", audio_sha256=audio_sha256
            )

        body = response.json()
        artifact = self.artifacts.write(
            result_id,
            {
                "schema": 1,
                "result_id": str(result_id),
                "sweep_id": claim.sweep_id,
                "sweep_model_id": claim.sweep_model_id,
                "attempt": claim.attempt,
                "model": claim.model.logical_name,
                "model_sha256": claim.model.model_sha256,
                "segment": segment.model_dump(),
                "audio_sha256": audio_sha256,
                "request": {"params": params},
                "runtime": runtime.describe(),
                "http_status": response.status_code,
                "elapsed_ms": response.elapsed_ms,
                "response": body if body is not None else response.body.decode("utf-8", "replace"),
            },
        )
        stored = {
            "artifact_uri": artifact.uri,
            "artifact_sha256": artifact.sha256,
            "artifact_size_bytes": artifact.size_bytes,
            "audio_sha256": audio_sha256,
            "inference_ms": response.elapsed_ms,
        }
        if response.status_code != 200:
            text = response.body[:500].decode("utf-8", "replace")
            return error(f"http_{response.status_code}", text, **stored)
        if body is None:
            return error("invalid_response", "response is not a JSON object", **stored)

        parsed = interpret(body)
        duration = body.get("duration")
        audio_ms = (
            round(float(duration) * 1000)
            if isinstance(duration, int | float)
            else segment.duration_ms
        )
        return ResultPost(
            id=result_id,
            segment_id=segment.segment_id,
            status=ResultStatus.SUCCESS if parsed.text else ResultStatus.ABSTAINED,
            text=parsed.text,
            language=parsed.language,
            audio_ms=audio_ms,
            has_word_timestamps=parsed.has_word_timestamps,
            has_token_confidence=parsed.has_token_confidence,
            mean_token_confidence=parsed.mean_token_confidence,
            word_count=parsed.word_count,
            words=parsed.words,
            lease_seconds=self.tc.lease_seconds,
            **stored,
        )
