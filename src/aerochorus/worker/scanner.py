"""Corpus scanning: observe a mounted source and report to the control plane.

Safety properties:
* files are only read (``ReadOnlyCorpusReader``);
* every batch is committed by the API as it arrives, so an interrupted scan
  keeps its progress and the next scan simply continues;
* a directory's unseen segments are marked missing only after that directory
  was listed completely; if the source becomes unavailable mid-scan the scan
  ends as ``source_unavailable`` and nothing is marked missing.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import posixpath
import time
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from aerochorus.contracts import (
    DirectoryBatch,
    DirectoryState,
    FileObservation,
    ScanCreate,
    ScanFinish,
    ScanMode,
    ScanRead,
    ScanStatus,
    SegmentStat,
    SourceRead,
)
from aerochorus.corpus.paths import InvalidRelativePath, join_relative, normalize_relative_dir
from aerochorus.worker.audio import probe_audio
from aerochorus.worker.client import ApiClient
from aerochorus.worker.config import WorkerConfig
from aerochorus.worker.fs import FileEntry, ReadOnlyCorpusReader, SourceUnavailable
from aerochorus.worker.health import collect_health, heartbeat_from_health

log = logging.getLogger(__name__)

SEEN_CHUNK = 5000
MAX_ERRORS = 200


class WorkerConfigError(RuntimeError):
    pass


ReaderFactory = Callable[[Path, Sequence[str]], ReadOnlyCorpusReader]


class CorpusScanner:
    def __init__(
        self,
        client: ApiClient,
        config: WorkerConfig,
        *,
        clock: Callable[[], float] = time.time,
        reader_factory: ReaderFactory = ReadOnlyCorpusReader,
    ) -> None:
        self.client = client
        self.config = config
        self.clock = clock
        self.reader_factory = reader_factory

    def scan(
        self, source_key: str, mode: ScanMode = ScanMode.INCREMENTAL, scope_prefix: str = ""
    ) -> ScanRead:
        mount = self.config.sources.get(source_key)
        if mount is None:
            raise WorkerConfigError(f"source {source_key!r} has no root configured on this machine")
        health = collect_health(self.config, self.client)
        self.client.heartbeat(heartbeat_from_health(self.config, health))
        source = self.client.get_source(source_key)
        reader = self.reader_factory(mount.root, source.adapter_config.sentinel_paths)
        scan = self.client.start_scan(
            ScanCreate(
                source_key=source_key,
                worker_name=self.config.worker_name,
                mode=mode,
                scope_prefix=normalize_relative_dir(scope_prefix),
            )
        )
        log.info(
            "scan %s started: %s mode=%s scope=%r", scan.id, source_key, mode, scan.scope_prefix
        )
        return _ScanRun(self, scan, source, reader).execute()


@dataclass
class _Read:
    relative_path: str
    known: bool
    observation: FileObservation | None = None
    # "vanished" | "changing" | "error"
    problem: str | None = None
    detail: str | None = None


class _ScanRun:
    def __init__(
        self,
        scanner: CorpusScanner,
        scan: ScanRead,
        source: SourceRead,
        reader: ReadOnlyCorpusReader,
    ) -> None:
        self.client = scanner.client
        self.config = scanner.config
        self.clock = scanner.clock
        self.scan = scan
        self.source = source
        self.adapter = source.adapter_config
        self.reader = reader
        self.mode = ScanMode(scan.mode)
        self.counters: Counter[str] = Counter()
        self.errors: list[str] = []
        self.pool: ThreadPoolExecutor

    # -- lifecycle ------------------------------------------------------------

    def execute(self) -> ScanRead:
        try:
            availability = self.reader.check_available()
            if not availability.ok:
                return self._finish(ScanStatus.SOURCE_UNAVAILABLE, availability.reason)
            scope = self.scan.scope_prefix
            if scope and not self.reader.is_dir(scope):
                return self._finish(ScanStatus.FAILED, f"scope {scope!r} is not a directory")

            states: dict[str, DirectoryState] = {}
            if self.mode == ScanMode.INCREMENTAL:
                states = {
                    s.relative_dir: s for s in self.client.directory_states(self.source.logical_key)
                }
            with ThreadPoolExecutor(self.config.read_concurrency) as pool:
                self.pool = pool
                stack = [scope]
                while stack:
                    relative_dir = stack.pop()
                    subdirs = self._visit(relative_dir, states.get(relative_dir))
                    stack.extend(
                        join_relative(relative_dir, s) for s in sorted(subdirs, reverse=True)
                    )
            return self._finish(ScanStatus.COMPLETED)
        except OSError as exc:
            availability = self.reader.check_available()
            if isinstance(exc, SourceUnavailable) or not availability.ok:
                reason = availability.reason or str(exc)
                return self._finish(ScanStatus.SOURCE_UNAVAILABLE, f"{reason} (during scan: {exc})")
            return self._finish(ScanStatus.FAILED, f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            with contextlib.suppress(Exception):
                self._finish(ScanStatus.FAILED, f"{type(exc).__name__}: {exc}")
            raise

    def _finish(self, status: ScanStatus, message: str | None = None) -> ScanRead:
        result = self.client.finish_scan(
            self.scan.id,
            ScanFinish(
                status=status,
                error_message=message,
                counters=dict(self.counters),
                errors=self.errors[:MAX_ERRORS],
            ),
        )
        log.info("scan %s finished: %s %s", result.id, result.status, message or "")
        return result

    def _error(self, message: str) -> None:
        self.counters["errors"] += 1
        if len(self.errors) < MAX_ERRORS:
            self.errors.append(message)

    # -- directories ------------------------------------------------------------

    def _visit(self, relative_dir: str, state: DirectoryState | None) -> list[str]:
        mtime_ns = self.reader.dir_mtime_ns(relative_dir)
        if (
            self.mode == ScanMode.INCREMENTAL
            and state is not None
            and state.settled
            and state.mtime_ns == mtime_ns
        ):
            self.counters["dirs_skipped"] += 1
            return list(state.subdirs)

        listing = self.reader.list_directory(relative_dir)
        self.counters["dirs_listed"] += 1
        self.counters["entries_skipped"] += len(listing.skipped)
        now = self.clock()
        min_age = self.adapter.min_file_age_seconds
        # A recently modified directory may still be gaining entries (and remote
        # filesystems may cache listings), so do not trust it for skipping yet.
        settled = now - listing.mtime_ns / 1e9 >= min_age

        subdirs = []
        for name in listing.subdirs:
            try:
                join_relative(relative_dir, name)
                subdirs.append(name)
            except InvalidRelativePath as exc:
                self._error(f"{relative_dir}/{name}: {exc}")

        known: dict[str, SegmentStat] = {}
        if listing.files:
            known = {
                s.relative_path: s
                for s in self.client.segment_stats(self.source.logical_key, relative_dir)
            }

        seen: list[str] = []
        to_read: list[tuple[str, FileEntry, bool]] = []
        eligible = 0
        for entry in listing.files:
            if posixpath.splitext(entry.name)[1].lower() not in self.adapter.include_extensions:
                self.counters["files_ignored"] += 1
                continue
            try:
                relative_path = join_relative(relative_dir, entry.name)
            except InvalidRelativePath as exc:
                self._error(f"{relative_dir}/{entry.name}: {exc}")
                continue
            eligible += 1
            stored = known.get(relative_path)
            if now - entry.mtime_ns / 1e9 < min_age:
                self.counters["files_unsettled"] += 1
                settled = False
                if stored is not None:
                    seen.append(relative_path)
                continue
            if (
                self.mode != ScanMode.VERIFY
                and stored is not None
                and stored.file_size == entry.size
                and stored.file_mtime_ns == entry.mtime_ns
            ):
                self.counters["files_unchanged"] += 1
                seen.append(relative_path)
                continue
            to_read.append((relative_path, entry, stored is not None))

        observations: list[FileObservation] = []
        for read in self.pool.map(self._read, to_read):
            if read.observation is not None:
                self.counters["files_read"] += 1
                observations.append(read.observation)
            else:
                self.counters[f"files_{read.problem}"] += 1
                if read.problem != "vanished":
                    settled = False
                    if read.known:
                        seen.append(read.relative_path)
                if read.detail:
                    self._error(f"{read.relative_path}: {read.detail}")
            if len(observations) >= self.config.observation_batch_size:
                self._post(DirectoryBatch(relative_dir=relative_dir, observations=observations))
                observations = []

        seen_chunks = [seen[i : i + SEEN_CHUNK] for i in range(0, len(seen), SEEN_CHUNK)] or [[]]
        for chunk in seen_chunks[:-1]:
            self._post(DirectoryBatch(relative_dir=relative_dir, seen=chunk))
        self._post(
            DirectoryBatch(
                relative_dir=relative_dir,
                observations=observations,
                seen=seen_chunks[-1],
                final=True,
                directory_mtime_ns=listing.mtime_ns,
                settled=settled,
                subdirs=subdirs,
                file_count=eligible,
            )
        )
        log.info(
            "%s: %d files, %d to read, %d unchanged or unsettled",
            relative_dir or "<root>",
            eligible,
            len(to_read),
            eligible - len(to_read),
        )
        return subdirs

    def _post(self, batch: DirectoryBatch) -> None:
        self.client.post_batch(self.scan.id, batch)

    # -- files ------------------------------------------------------------------

    def _read(self, item: tuple[str, FileEntry, bool]) -> _Read:
        relative_path, entry, known = item
        try:
            data = self.reader.read_bytes(relative_path)
        except FileNotFoundError:
            self.reader.require_available()
            return _Read(relative_path, known, problem="vanished")
        except OSError as exc:
            self.reader.require_available()
            return _Read(
                relative_path, known, problem="error", detail=f"{type(exc).__name__}: {exc}"
            )
        if len(data) != entry.size:
            return _Read(relative_path, known, problem="changing")
        return _Read(
            relative_path,
            known,
            observation=FileObservation(
                relative_path=relative_path,
                file_size=entry.size,
                file_mtime_ns=entry.mtime_ns,
                sha256=hashlib.sha256(data).hexdigest(),
                audio=probe_audio(data, posixpath.splitext(relative_path)[1]),
            ),
        )
