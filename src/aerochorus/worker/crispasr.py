"""CrispASR process lifecycle and HTTP client (ADR-005, ADR-006).

One server per model: start it with the model loaded, transcribe the whole
window through it, stop it. Two launchers share the same HTTP contract:

* ``native`` spawns the ``crispasr`` binary (macOS Metal; Windows/Linux CUDA);
* ``docker`` runs the published CrispASR server image with GPU passthrough.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from aerochorus.worker.config import CrispAsrConfig

log = logging.getLogger(__name__)


class CrispAsrError(RuntimeError):
    pass


def _parse_build_info(output: str) -> dict[str, str]:
    """Parse ``crispasr --version`` ("  version       : 0.8.37" lines)."""
    info = {}
    for line in output.splitlines():
        match = re.match(r"^\s*([A-Za-z][\w /]*?)\s*:\s*(.+?)\s*$", line)
        if match:
            info[match[1].strip().replace(" ", "_").lower()] = match[2]
    return info


@dataclass(frozen=True)
class Runtime:
    launcher: str
    build: dict[str, str]
    # Identifies the exact executable: image digest or binary sha256.
    artifact: str

    @property
    def version(self) -> str:
        return self.build.get("version", "unknown")

    def describe(self) -> dict[str, Any]:
        return {
            "launcher": self.launcher,
            "crispasr_version": self.version,
            "build": self.build,
            "artifact": self.artifact,
        }

    def fingerprint(self, model_sha256: str, backend: str) -> str:
        """Same CrispASR build + same model artifact => comparable results."""
        identity = {
            "crispasr_version": self.version,
            "git_sha": self.build.get("git_sha"),
            "artifact": self.artifact,
            "model_sha256": model_sha256,
            "backend": backend,
        }
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


@dataclass
class TranscribeResponse:
    status_code: int
    body: bytes
    elapsed_ms: int

    def json(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.body)
        except (ValueError, UnicodeDecodeError):
            return None
        return value if isinstance(value, dict) else None


@dataclass
class Server:
    """A running CrispASR server with one model resident."""

    base_url: str
    request_timeout: float
    stop_fn: Any
    alive_fn: Any
    describe: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._http = httpx.Client(base_url=self.base_url, timeout=self.request_timeout)

    def health(self) -> tuple[int, dict[str, Any]]:
        response = self._http.get("/health", timeout=10)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, {}

    def wait_ready(self, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last = "no response"
        while time.monotonic() < deadline:
            if not self.alive_fn():
                raise CrispAsrError(f"CrispASR exited during startup ({last})")
            try:
                status, body = self.health()
                if status == 200 and body.get("status") == "ok":
                    return body
                last = f"HTTP {status} {body}"
            except httpx.TransportError as exc:
                last = type(exc).__name__
            time.sleep(1.0)
        raise CrispAsrError(f"CrispASR not ready after {timeout:.0f}s ({last})")

    def loaded_models(self) -> list[str]:
        with contextlib.suppress(httpx.HTTPError, ValueError):
            data = self._http.get("/v1/models", timeout=10).json().get("data", [])
            return [m.get("id", "") for m in data]
        return []

    def transcribe(self, audio: bytes, filename: str, params: dict[str, str]) -> TranscribeResponse:
        started = time.perf_counter()
        response = self._http.post(
            "/v1/audio/transcriptions",
            files={"file": (filename, audio, "application/octet-stream")},
            data=params,
        )
        elapsed = int((time.perf_counter() - started) * 1000)
        return TranscribeResponse(response.status_code, response.content, elapsed)

    def is_alive(self) -> bool:
        return bool(self.alive_fn())

    def stop(self) -> None:
        self._http.close()
        self.stop_fn()


class Launcher(Protocol):
    def runtime(self) -> Runtime: ...

    def start(
        self, model_path: Path, backend: str, language: str | None, log_path: Path
    ) -> Server: ...


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


class NativeLauncher:
    def __init__(self, config: CrispAsrConfig) -> None:
        if config.binary is None:
            raise CrispAsrError("crispasr.binary must be set for the native launcher")
        self.config = config
        self.binary = Path(config.binary)

    def runtime(self) -> Runtime:
        if not self.binary.is_file():
            raise CrispAsrError(f"CrispASR binary not found: {self.binary}")
        done = subprocess.run(
            [str(self.binary), "--version"], capture_output=True, text=True, timeout=120
        )
        return Runtime(
            launcher="native",
            build=_parse_build_info(done.stdout + done.stderr),
            artifact=f"sha256:{_hash_file(self.binary)}",
        )

    def start(self, model_path: Path, backend: str, language: str | None, log_path: Path) -> Server:
        args = [
            str(self.binary),
            "--server",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.config.host_port),
            "-m",
            str(model_path),
            "--backend",
            backend,
        ]
        if language:
            args += ["-l", language]
        args += self.config.extra_args
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "ab")  # noqa: SIM115 - owned by the server lifetime
        env = None
        if self.config.env:
            import os

            env = os.environ | self.config.env
        proc = subprocess.Popen(
            args, stdout=log_file, stderr=subprocess.STDOUT, cwd=self.binary.parent, env=env
        )
        log.info("started crispasr pid %s: %s", proc.pid, " ".join(args))

        def stop() -> None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=30)
            log_file.close()

        return Server(
            base_url=f"http://127.0.0.1:{self.config.host_port}",
            request_timeout=self.config.request_timeout_seconds,
            stop_fn=stop,
            alive_fn=lambda: proc.poll() is None,
            describe={"pid": proc.pid, "args": args},
        )


class DockerLauncher:
    CONTAINER_PORT = 8080

    def __init__(self, config: CrispAsrConfig) -> None:
        self.config = config
        self.name = f"aerochorus-crispasr-{config.host_port}"

    def _docker(self, *args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)

    def runtime(self) -> Runtime:
        inspect = self._docker("image", "inspect", self.config.image, "--format", "{{.Id}}")
        if inspect.returncode != 0:
            raise CrispAsrError(
                f"image {self.config.image} not present; run `docker pull {self.config.image}`"
            )
        gpu = ["--gpus", self.config.gpus] if self.config.gpus else []
        version = self._docker(
            "run",
            "--rm",
            *gpu,
            "--entrypoint",
            "crispasr",
            self.config.image,
            "--version",
            timeout=300,
        )
        return Runtime(
            launcher="docker",
            build=_parse_build_info(version.stdout + version.stderr) | {"image": self.config.image},
            artifact=inspect.stdout.strip(),
        )

    def start(self, model_path: Path, backend: str, language: str | None, log_path: Path) -> Server:
        self._docker("rm", "-f", self.name)
        extra = " ".join(self.config.extra_args)
        args = [
            "run",
            "-d",
            "--name",
            self.name,
            "-p",
            f"127.0.0.1:{self.config.host_port}:{self.CONTAINER_PORT}",
            "--mount",
            f"type=bind,source={model_path.parent},target=/models,readonly",
            "-e",
            f"CRISPASR_MODEL=/models/{model_path.name}",
            "-e",
            f"CRISPASR_BACKEND={backend}",
            "-e",
            f"CRISPASR_LANGUAGE={language or 'auto'}",
            "-e",
            "CRISPASR_AUTO_DOWNLOAD=0",
            "-e",
            f"CRISPASR_EXTRA_ARGS={extra}",
        ]
        for key, value in self.config.env.items():
            args += ["-e", f"{key}={value}"]
        if self.config.gpus:
            args += ["--gpus", self.config.gpus]
        args.append(self.config.image)
        started = self._docker(*args)
        if started.returncode != 0:
            raise CrispAsrError(f"docker run failed: {started.stderr.strip()}")
        log.info("started container %s (%s)", self.name, started.stdout.strip()[:12])

        def alive() -> bool:
            state = self._docker("inspect", "-f", "{{.State.Running}}", self.name)
            return state.returncode == 0 and state.stdout.strip() == "true"

        def stop() -> None:
            logs = self._docker("logs", self.name)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(logs.stdout + logs.stderr)
            self._docker("stop", "-t", "15", self.name, timeout=60)
            self._docker("rm", "-f", self.name)

        return Server(
            base_url=f"http://127.0.0.1:{self.config.host_port}",
            request_timeout=self.config.request_timeout_seconds,
            stop_fn=stop,
            alive_fn=alive,
            describe={"container": self.name, "image": self.config.image},
        )


def make_launcher(config: CrispAsrConfig) -> Launcher:
    return NativeLauncher(config) if config.launcher == "native" else DockerLauncher(config)


# --- response interpretation -------------------------------------------------------------


@dataclass(frozen=True)
class Interpretation:
    text: str
    language: str | None
    word_count: int
    has_word_timestamps: bool
    has_token_confidence: bool
    mean_token_confidence: float | None


def _probability(item: dict[str, Any]) -> float | None:
    for key in ("probability", "p", "confidence", "prob"):
        value = item.get(key)
        if isinstance(value, int | float):
            return float(value)
    return None


def interpret(body: dict[str, Any]) -> Interpretation:
    """Extract queryable fields from a verbose_json response.

    Backends differ in what they return (ADR-008), so every auxiliary field
    is optional. Probabilities are kept per model: they are not comparable
    across models and are never pooled here.
    """
    text = str(body.get("text") or "").strip()
    segments = [s for s in body.get("segments") or [] if isinstance(s, dict)]
    words = [w for s in segments for w in (s.get("words") or []) if isinstance(w, dict)]
    tokens = [t for s in segments for t in (s.get("tokens") or []) if isinstance(t, dict)]
    timed = [w for w in words if ("start" in w and "end" in w) or ("t0" in w and "t1" in w)]
    probabilities = [p for p in map(_probability, tokens or words) if p is not None]
    language = body.get("language")
    return Interpretation(
        text=text,
        language=language if isinstance(language, str) and language else None,
        word_count=len(words) if words else len(text.split()),
        has_word_timestamps=bool(words) and len(timed) == len(words),
        has_token_confidence=bool(probabilities),
        mean_token_confidence=(
            round(sum(probabilities) / len(probabilities), 6) if probabilities else None
        ),
    )
