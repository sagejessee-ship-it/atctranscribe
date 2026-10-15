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
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
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
from aerochorus.worker import hardware
from aerochorus.worker.artifacts import ArtifactStore
from aerochorus.worker.client import ApiClient, ApiError
from aerochorus.worker.config import ModelRuntime, WorkerConfig
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
# something is wrong with the server, not with the audio. A segment that already failed
# in an earlier attempt (``PendingSegment.retry``) and fails again is not counted.
MAX_CONSECUTIVE_ERRORS = 5
# A single segment can crash a CrispASR backend (e.g. a GGML assertion on odd audio), or
# outlast the request timeout (fastconformer on a 16-minute recording): CrispASR keeps
# working on a request the worker gave up on, so every later request would time out
# behind it. Either costs that segment an error result and the server a restart; after
# this many restarts in one model run the backend is treated as unstable. Restarts after
# a segment that already failed in an earlier attempt are not counted.
MAX_RESTARTS = 3
# The first request is a smoke test of the server. If it fails, the next segments are
# tried as the smoke test; after this many the backend is judged broken and the model
# run fails without recording any results. A retry starts with the segments that failed
# before; those are recorded as usual and the first fresh segment is the smoke test.
MAX_SMOKE_SEGMENTS = 3
# A progress line in the log this often, so a long model run visibly moves.
PROGRESS_EVERY_S = 300.0


def _is_within(child: Path, parent: Path) -> bool:
    child_s = os.path.normcase(os.path.abspath(child))
    parent_s = os.path.normcase(os.path.abspath(parent))
    try:
        return os.path.commonpath([child_s, parent_s]) == parent_s
    except ValueError:  # different drives / UNC hosts
        return False


class SlotRegistry:
    """Model runs the slots of one worker hold, and whether each model is fully loaded."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: dict[int, tuple[int, bool]] = {}  # slot -> (sweep_model_id, loaded)

    def hold(self, slot: int, sweep_model_id: int) -> None:
        with self._lock:
            self._held[slot] = (sweep_model_id, False)

    def loaded(self, slot: int) -> None:
        with self._lock:
            if slot in self._held:
                self._held[slot] = (self._held[slot][0], True)

    def release(self, slot: int) -> None:
        with self._lock:
            self._held.pop(slot, None)

    def others(self, slot: int) -> list[tuple[int, bool]]:
        with self._lock:
            return [held for s, held in self._held.items() if s != slot]


class SmokeFailed(RuntimeError):
    """The server could not answer a smoke-test request (``result`` is that segment's error)."""

    def __init__(self, message: str, result: ResultPost | None = None) -> None:
        super().__init__(message)
        self.result = result


class Released(Exception):
    """The model run was handed back to the queue (pause, cancel, shutdown)."""


class _SegmentFeed:
    """A model run's pending segments, fetched in batches behind one cursor and handed
    out once each. Thread-safe: several server copies draw from it. It also releases the
    run (once) when the worker stops or the sweep is no longer running."""

    def __init__(self, worker: SweepWorker, claim: ModelRunClaim) -> None:
        self.worker = worker
        self.claim = claim
        self.lock = threading.Lock()
        self.buffer: deque[PendingSegment] = deque()
        self.cursor = -1  # the highest sweep ordinal handed out in this claim
        self.closed = False

    def close(self) -> None:
        with self.lock:
            self.closed = True

    def _release(self, reason: str) -> Released:
        self.closed = True
        self.worker.client.release_model_run(
            self.claim.sweep_model_id, self.worker.config.worker_name, reason
        )
        return Released(reason)

    def next(self) -> PendingSegment | None:
        with self.lock:
            if self.closed:
                return None
            w = self.worker
            if w.stop.is_set():
                raise self._release("worker stopping")
            if not self.buffer:
                batch = w.client.pending_segments(
                    self.claim.sweep_model_id, w.tc.pending_batch, after_ordinal=self.cursor
                )
                if batch.sweep_status != SweepStatus.RUNNING or (
                    batch.model_status != ModelRunStatus.RUNNING
                ):
                    raise self._release(f"sweep is {batch.sweep_status}")
                if not batch.segments:
                    self.closed = True
                    return None
                ordinals = [s.ordinal for s in batch.segments if s.ordinal is not None]
                if ordinals:
                    self.cursor = max(self.cursor, *ordinals)
                self.buffer.extend(batch.segments)
            return self.buffer.popleft()


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
        slot: int = 0,
        registry: SlotRegistry | None = None,
    ) -> None:
        if config.transcription is None:
            raise CrispAsrError("this worker has no [transcription] configuration")
        self.client = client
        self.config = config
        self.tc = config.transcription
        self.slot = slot
        self.registry = registry or SlotRegistry()
        self.store = ModelStore(self.tc.models_dir)
        self.artifacts = ArtifactStore(self.tc.artifact_root, self.tc.artifact_store)
        # Each slot runs its own CrispASR server: its own port (and container name).
        crisp = self.tc.crispasr.model_copy(update={"host_port": self.tc.crispasr.host_port + slot})
        self.launcher = launcher or make_launcher(crisp)
        self._shared_launcher = launcher is not None  # injected (tests): one for every copy
        self._copy_launchers: dict[int, Launcher] = {}
        self.hardware_profile = resolve_profile(config.hardware_profile)
        self.stop = stop or threading.Event()
        self.readers = {
            key: ReadOnlyCorpusReader(mount.root) for key, mount in config.sources.items()
        }
        self._unreadable: set[str] = set()
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

    def _vram_budget(self) -> tuple[list[int], int | None] | None:
        """(model runs held by the other slots, GPU memory this slot may use), or None
        when this slot should not start anything now."""
        others = self.registry.others(self.slot)
        if not others:
            # Alone on the GPU: the first slot takes whatever comes next, as always. An
            # extra slot only ever joins a model that is already running.
            return ([], None) if self.slot == 0 else None
        if not all(loaded for _, loaded in others):
            return None  # another model is still loading: its memory is not visible yet
        free = hardware.gpu_memory_free_mb()
        if free is None or free - self.tc.vram_headroom_mb < 256:
            return None
        return [held for held, _ in others], free - self.tc.vram_headroom_mb

    def _launcher_for(self, copy: int) -> Launcher:
        """Copy 0 is the slot's own server. Further copies of the same model get their own
        port and container: host_port + slot + 2 x copy (8090/8092/... for slot 0,
        8091/8093/... for slot 1)."""
        if copy == 0 or self._shared_launcher:
            return self.launcher
        if copy not in self._copy_launchers:
            port = self.tc.crispasr.host_port + self.slot + 2 * copy
            self._copy_launchers[copy] = make_launcher(
                self.tc.crispasr.model_copy(update={"host_port": port})
            )
        return self._copy_launchers[copy]

    def _room_for_copy(self, per_copy_mb: int) -> tuple[bool, int | None]:
        """(may another copy start, free GPU memory in MB). Never while another slot's
        model is still loading: its memory is not visible yet."""
        if any(not loaded for _, loaded in self.registry.others(self.slot)):
            return False, None
        free = hardware.gpu_memory_free_mb()
        if free is None:
            return False, None
        return free - self.tc.vram_headroom_mb >= per_copy_mb, free

    def _readable_sources(self) -> list[str]:
        """Sources this worker can read right now. Runs over a share that is down are not
        claimed: loading a model only to hand the run straight back would repeat every
        minute until the share returns."""
        readable = []
        for key, reader in sorted(self.readers.items()):
            check = reader.check_available()
            if check.ok:
                readable.append(key)
                if key in self._unreadable:
                    self._unreadable.discard(key)
                    log.info("source %s is readable again; its runs continue", key)
            elif key not in self._unreadable:
                self._unreadable.add(key)
                log.warning("source %s unavailable (%s); its runs wait", key, check.reason)
        return readable

    def process_next(self) -> Outcome | None:
        budget = self._vram_budget()
        if budget is None:
            return None
        sources = self._readable_sources()
        if not sources:
            return None
        exclude, max_vram = budget
        claim = self.client.claim_model_run(
            ClaimRequest(
                worker_name=self.config.worker_name,
                lease_seconds=self.tc.lease_seconds,
                source_keys=sources,
                hardware_profile=self.hardware_profile,
                exclude_model_runs=exclude,
                max_vram_mb=max_vram,
            )
        )
        if claim is None:
            return None
        if max_vram is not None:
            log.info(
                "slot %s: %s runs alongside another model (%s MB of GPU memory available)",
                self.slot,
                claim.model.logical_name,
                max_vram,
            )
        log.info(
            "claimed sweep %s model %s (attempt %s, %s/%s already recorded)",
            claim.sweep_id,
            claim.model.logical_name,
            claim.attempt,
            claim.results_recorded,
            claim.segments_total,
        )
        self.registry.hold(self.slot, claim.sweep_model_id)
        try:
            with keep_awake(f"sweep {claim.sweep_id} {claim.model.logical_name}"):
                return self._process(claim)
        finally:
            self.registry.release(self.slot)

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
        servers: list[Server] = []
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
            log_name = f"sweep{claim.sweep_id}-{model.logical_name}"

            def start(copy: int) -> tuple[Server, dict[str, Any]]:
                """One copy of the model's server, loaded and verified."""
                suffix = f"-copy{copy}" if copy else ""
                server = self._launcher_for(copy).start(
                    model_path,
                    model.crisp_backend,
                    claim.request_params.get("language"),
                    self.log_dir / f"{log_name}{suffix}.log",
                    overrides=overrides,
                )
                servers.append(server)  # stopped in `finally` whatever happens next
                health = server.wait_ready(self.tc.crispasr.startup_timeout_seconds)
                self._verify_identity(claim, health, server)
                return server, health

            started = time.monotonic()
            free_before = (
                hardware.gpu_memory_free_mb() if (overrides and overrides.replicas > 1) else None
            )
            server, health = start(0)
            load_s = round(time.monotonic() - started, 1)
            log.info("%s loaded in %ss (%s)", model.logical_name, load_s, health)
            copies = self._start_copies(claim, overrides, free_before, start)
            self.registry.loaded(self.slot)

            def restart(copy: int) -> Server:
                """A fresh server for one copy after a crash (same runtime and settings)."""
                old = servers[copy]
                old.stop()
                server = self._launcher_for(copy).start(
                    model_path,
                    model.crisp_backend,
                    claim.request_params.get("language"),
                    self.log_dir / f"{log_name}{f'-copy{copy}' if copy else ''}.log",
                    overrides=overrides,
                )
                servers[copy] = server
                self._verify_identity(
                    claim, server.wait_ready(self.tc.crispasr.startup_timeout_seconds), server
                )
                return server

            self.client.start_model_run(
                claim.sweep_model_id,
                ModelRunStart(
                    worker_name=self.config.worker_name,
                    runtime=runtime.describe()
                    | {
                        "load_seconds": load_s,
                        "server": server.describe,
                        "copies": copies,
                        "health": health,
                        "memory": memory,
                        "hardware_profile": self.hardware_profile,
                    },
                    runtime_fingerprint=fingerprint,
                    lease_seconds=self.tc.lease_seconds,
                ),
            )
            self._serve(claim, servers, runtime, outcome, restart)
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
            for server in servers:
                server.stop()
        log.info(
            "model run %s (%s): %s after %s segments%s",
            claim.sweep_model_id,
            model.logical_name,
            outcome.status,
            outcome.processed,
            f" ({outcome.message})" if outcome.status == "released" and outcome.message else "",
        )
        return outcome

    def _start_copies(
        self,
        claim: ModelRunClaim,
        overrides: ModelRuntime | None,
        free_before: int | None,
        start: Callable[[int], tuple[Server, dict[str, Any]]],
    ) -> int:
        """Start further copies of the model's server, as configured and as memory allows.
        A copy needs what the first one took (measured), else an estimate from the
        artifact size. Returns how many copies serve this run."""
        wanted = overrides.replicas if overrides else 1
        if wanted <= 1:
            return 1
        model = claim.model
        estimate = round((model.artifact_size_bytes or 0) / 2**20 * 1.25) + 500
        free_after = hardware.gpu_memory_free_mb()
        measured = free_before - free_after if free_before and free_after else 0
        per_copy = measured if measured > 0 else estimate
        copies = 1
        while copies < wanted:
            room, free = self._room_for_copy(per_copy)
            if not room:
                log.info(
                    "%s: %s of %s copies (another needs ~%s MB; %s MB free, %s MB headroom)",
                    model.logical_name, copies, wanted, per_copy, free, self.tc.vram_headroom_mb,
                )  # fmt: skip
                break
            start(copies)
            copies += 1
        if copies > 1:
            log.info(
                "%s: %s copies of the server share this model run (~%s MB each)",
                model.logical_name, copies, per_copy,
            )  # fmt: skip
        return copies

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

    @staticmethod
    def _exited(server: Server, error_type: str | None) -> bool:
        """Did the server die? A dropped connection is given a moment to show a crash."""
        if not server.is_alive():
            return True
        if error_type != "server_unavailable":
            return False
        for _ in range(12):
            time.sleep(0.25)
            if not server.is_alive():
                return True
        return False

    @classmethod
    def _must_restart(cls, server: Server, error_type: str | None) -> bool:
        """Did the server die, or is it still busy with a request that timed out?"""
        return error_type == "timeout" or cls._exited(server, error_type)

    def _serve(
        self,
        claim: ModelRunClaim,
        servers: list[Server],
        runtime: Runtime,
        outcome: Outcome,
        restart: Callable[[int], Server],
    ) -> None:
        """Transcribe the run's pending segments on one server, or on several identical
        copies at once. One feed (one cursor) hands each segment to exactly one copy. Each
        copy keeps its own smoke test and error streak; the restart budget is shared."""
        lock = threading.Lock()
        feed = _SegmentFeed(self, claim)
        shared = {"restarts": 0, "works": False}
        progress = {"at": time.monotonic(), "processed": 0, "errors": 0}

        def post(result: ResultPost) -> None:
            ack = self.client.post_result(claim.sweep_model_id, self.config.worker_name, result)
            with lock:
                outcome.processed += 1
                progress["errors"] += result.status == ResultStatus.ERROR
                now = time.monotonic()
                if now - progress["at"] < PROGRESS_EVERY_S:
                    return
                rate = (outcome.processed - progress["processed"]) / (now - progress["at"])
                progress["at"], progress["processed"] = now, outcome.processed
                errors = progress["errors"]
            left = ack.segments_total - ack.segments_recorded
            log.info(
                "%s (sweep %s): %s/%s done, %.2f segments/s, %s errors so far, "
                "~%.1f h left for this model",
                claim.model.logical_name,
                claim.sweep_id,
                f"{ack.segments_recorded:,}",
                f"{ack.segments_total:,}",
                rate,
                errors,
                left / rate / 3600 if rate else float("nan"),
            )

        def lane(copy: int) -> None:
            """One server copy: draw segments until the feed runs dry (the sequential loop
            when there is only one copy)."""
            server = servers[copy]
            consecutive_errors = 0
            smoke_done = False
            # Segments that failed as this copy's smoke test: recorded only once the
            # server proves to work (then it was the audio), otherwise the run fails clean.
            held: list[ResultPost] = []

            def recover(segment: PendingSegment, result: ResultPost) -> Server:
                nonlocal smoke_done
                with lock:
                    if not segment.retry:  # a known failure says nothing new about the backend
                        shared["restarts"] += 1
                    restarts = shared["restarts"]
                if restarts > MAX_RESTARTS:
                    raise CrispAsrError(
                        f"CrispASR restarted {restarts} times in this model run; last on "
                        f"{segment.relative_path}: {result.error_type}: {result.error_message}"
                    )
                log.warning(
                    "CrispASR %s on %s (%s); restarting %s(%s/%s)",
                    "timed out" if result.error_type == "timeout" else "exited",
                    segment.relative_path,
                    result.error_message,
                    f"copy {copy} " if len(servers) > 1 else "",
                    restarts,
                    MAX_RESTARTS,
                )
                smoke_done = False  # the restarted server is smoke-tested again
                return restart(copy)

            while (segment := feed.next()) is not None:
                try:
                    result = self._transcribe(
                        claim, server, runtime, segment, smoke=not (smoke_done or segment.retry)
                    )
                except SmokeFailed as exc:
                    if exc.result is None:
                        raise
                    held.append(exc.result)
                    if len(held) >= MAX_SMOKE_SEGMENTS:
                        raise SmokeFailed(
                            f"smoke test failed on {len(held)} segments in a row; last: {exc}"
                        ) from exc
                    log.warning("%s; trying the next segment as the smoke test", exc)
                    if self._must_restart(server, exc.result.error_type):
                        server = recover(segment, exc.result)
                    continue
                if not smoke_done and result.status != ResultStatus.ERROR:
                    smoke_done = True
                    shared["works"] = True
                    for earlier in held:  # the server works: those segments made it fail
                        post(earlier)
                    held.clear()
                post(result)
                if result.status == ResultStatus.ERROR:
                    if not segment.retry:
                        consecutive_errors += 1
                    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        raise CrispAsrError(
                            f"{consecutive_errors} consecutive errors; last: "
                            f"{result.error_type}: {result.error_message}"
                        )
                    if self._must_restart(server, result.error_type):
                        server = recover(segment, result)
                else:
                    consecutive_errors = 0
            if held:
                if len(servers) > 1 and shared["works"]:
                    # Another, identical copy proved the model works: it was the audio.
                    for earlier in held:
                        post(earlier)
                    return
                raise SmokeFailed(
                    f"smoke test: no remaining segment answered ({len(held)} failed); "
                    f"last: {held[-1].error_type}: {held[-1].error_message}"
                )

        if len(servers) == 1:
            lane(0)
            return
        with ThreadPoolExecutor(len(servers), thread_name_prefix=f"slot{self.slot}-copy") as pool:
            futures = [pool.submit(lane, copy) for copy in range(len(servers))]
            pending = set(futures)
            while pending:
                done, pending = wait(pending, return_when="FIRST_EXCEPTION")
                if any(f.exception() is not None for f in done):
                    feed.close()  # the others finish their current segment and stop
        errors = [f.exception() for f in futures if f.exception() is not None]
        if errors:
            # A failure outranks the release that may have stopped the other copies.
            raise next((e for e in errors if not isinstance(e, Released)), errors[0])

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
            result = ResultPost(
                id=result_id,
                segment_id=segment.segment_id,
                status=ResultStatus.ERROR,
                error_type=kind,
                error_message=message[:2000],
                lease_seconds=self.tc.lease_seconds,
                **extra,
            )
            # The first request that reaches the server is the smoke test (see _serve).
            if smoke and server_side:
                raise SmokeFailed(
                    f"smoke segment {segment.relative_path}: {kind}: {message}", result
                )
            return result

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
