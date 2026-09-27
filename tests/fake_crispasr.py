"""A scriptable stand-in for a CrispASR server, for tests without a GPU."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aerochorus.sweep_contracts import CatalogFamily, CatalogModel, CatalogSuite, CatalogSync
from aerochorus.worker.crispasr import CrispAsrError, Runtime, TranscribeResponse

Script = Callable[[str, dict[str, str], "FakeServer"], TranscribeResponse]


def ok(text: str, **extra: Any) -> TranscribeResponse:
    words = [
        {"word": w, "start": 0.1 * i, "end": 0.1 * i + 0.08} for i, w in enumerate(text.split())
    ]
    body = {
        "task": "transcribe",
        "language": extra.pop("language", "en"),
        "duration": extra.pop("duration", 2.5),
        "text": text,
        "segments": [{"id": 0, "text": text, "words": words}] if text else [],
    } | extra
    return TranscribeResponse(200, json.dumps(body).encode(), 40)


def default_script(filename: str, params: dict[str, str], server: FakeServer) -> TranscribeResponse:
    return ok(f"{server.backend} heard {filename.split('_')[1].lower()} traffic")


@dataclass
class FakeServer:
    backend: str
    model_path: Path
    script: Script
    alive: bool = True
    requests: list[str] = field(default_factory=list)
    describe: dict[str, Any] = field(default_factory=dict)
    fail_startup: bool = False

    def wait_ready(self, timeout: float) -> dict[str, Any]:
        if self.fail_startup:
            raise CrispAsrError("model failed to load (fake)")
        return {"status": "ok", "backend": self.backend}

    def loaded_models(self) -> list[str]:
        return [f"/models/{self.model_path.name}"]

    def transcribe(self, audio: bytes, filename: str, params: dict[str, str]):
        self.requests.append(filename)
        return self.script(filename, params, self)

    def is_alive(self) -> bool:
        return self.alive

    def stop(self) -> None:
        self.alive = False


@dataclass
class FakeLauncher:
    script: Script = default_script
    version: str = "0.0.0-fake"
    fail_startup_for: set[str] = field(default_factory=set)
    servers: list[FakeServer] = field(default_factory=list)

    def runtime(self) -> Runtime:
        return Runtime("fake", {"version": self.version, "git_sha": "test"}, "sha256:fake")

    def start(
        self,
        model_path: Path,
        backend: str,
        language: str | None,
        log_path: Path,
        overrides=None,
    ):
        server = FakeServer(
            backend=backend,
            model_path=model_path,
            script=self.script,
            fail_startup=backend in self.fail_startup_for,
        )
        self.servers.append(server)
        return server


def fake_catalog(models_dir: Path, names=("alpha", "beta")) -> CatalogSync:
    """Two tiny 'models' in different families, with real files and hashes."""
    models_dir.mkdir(parents=True, exist_ok=True)
    families, models = [], []
    for name in names:
        blob = f"fake gguf for {name}".encode()
        (models_dir / f"{name}.gguf").write_bytes(blob)
        families.append(CatalogFamily(key=f"family-{name}", display_name=name))
        models.append(
            CatalogModel(
                logical_name=f"{name}-model",
                architecture_family=f"family-{name}",
                crisp_backend=name,
                upstream_model=f"test/{name}",
                artifact_repo=f"test/{name}-GGUF",
                upstream_revision="0" * 40,
                model_filename=f"{name}.gguf",
                model_sha256=hashlib.sha256(blob).hexdigest(),
                artifact_size_bytes=len(blob),
                request_params={"language": "en"},
                experimental=False,
            )
        )
    suites = [CatalogSuite(name="pair", models=[m.logical_name for m in models])]
    return CatalogSync(families=families, models=models, suites=suites)
