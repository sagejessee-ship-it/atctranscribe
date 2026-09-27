"""Helpers shared by the PostgreSQL-backed tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from aerochorus.worker.client import ApiClient
from aerochorus.worker.config import SourceMount, WorkerConfig
from aerochorus.worker.scanner import CorpusScanner

SOURCE_KEY = "home_atc_archive"
# Far enough in the future that every fixture file counts as settled.
LATER = datetime(2030, 1, 1, tzinfo=UTC).timestamp()


def make_scanner(api: ApiClient, root: Path, *, clock: float = LATER, **reader) -> CorpusScanner:
    config = WorkerConfig(
        worker_name="test-worker",
        api_url="http://testserver",
        read_concurrency=2,
        observation_batch_size=2,
        sources={SOURCE_KEY: SourceMount(root=root)},
    )
    return CorpusScanner(api, config, clock=lambda: clock, **reader)
