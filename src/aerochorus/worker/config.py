"""Machine-local worker configuration.

This is where logical corpus sources are mapped to paths on *this* machine
(ADR-002). The same source can be ``/Volumes/ATC`` on the Mac and
``\\\\nas\\bwi`` on a Windows box without changing any stored identity.
"""

from __future__ import annotations

import os
import socket
import sys
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CONFIG_ENV = "AEROCHORUS_WORKER_CONFIG"


class SourceMount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: Path
    # Scan this source periodically from ``aerochorus worker run``.
    auto_scan: bool = False


class CrispAsrConfig(BaseModel):
    """How this machine runs CrispASR (ADR-005: native on the Mac)."""

    model_config = ConfigDict(extra="forbid")

    # "native": spawn the crispasr binary (macOS Metal, Windows/Linux CUDA builds).
    # "docker": run the CrispASR server image (Linux/Windows hosts with NVIDIA GPUs).
    launcher: Literal["native", "docker"] = "native"
    binary: Path | None = None
    image: str = "ghcr.io/crispstrobe/crispasr:main-cuda"
    gpus: str | None = "all"
    host_port: int = 8090
    extra_args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    startup_timeout_seconds: float = 900.0
    request_timeout_seconds: float = 600.0


class TranscriptionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    # Where verified model artifacts live on this machine.
    models_dir: Path
    # Where raw CrispASR responses are stored (ADR-004). Never inside a corpus source.
    artifact_root: Path
    # Logical name of this artifact store; part of every artifact URI.
    artifact_store: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    # Read-only copies of other machines' artifact stores (store -> local dir),
    # e.g. {"jesseepc" = "/Volumes/lab/jesseepc-artifacts"} on the Mac.
    artifact_mounts: dict[str, Path] = Field(default_factory=dict)
    crispasr: CrispAsrConfig = Field(default_factory=CrispAsrConfig)
    lease_seconds: int = Field(default=600, ge=30)
    pending_batch: int = Field(default=25, ge=1, le=1000)
    # Download a missing model when a sweep needs it (otherwise: `worker models pull`).
    auto_pull_models: bool = False


class WorkerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_name: str = Field(default_factory=socket.gethostname)
    api_url: str = "http://127.0.0.1:8000"
    heartbeat_interval_seconds: float = 30.0
    incremental_scan_interval_seconds: float = 900.0
    full_scan_interval_seconds: float = 86_400.0
    read_concurrency: int = Field(default=4, ge=1, le=32)
    observation_batch_size: int = Field(default=500, ge=1, le=5000)
    sources: dict[str, SourceMount] = Field(default_factory=dict)
    # Present on machines that transcribe.
    transcription: TranscriptionConfig | None = None
    # `worker run` also processes queued sweeps when transcription is configured.
    process_sweeps: bool = True


def default_config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "aerochorus" / "worker.toml"


def resolve_config_path(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit
    if env := os.environ.get(CONFIG_ENV):
        return Path(env)
    return default_config_path()


def load_worker_config(path: Path | None = None) -> WorkerConfig:
    path = resolve_config_path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"worker config not found at {path} (set {CONFIG_ENV} or pass --config); "
            "see config/worker.example.toml"
        )
    with path.open("rb") as fh:
        return WorkerConfig.model_validate(tomllib.load(fh))
