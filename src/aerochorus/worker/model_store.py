"""Model artifacts on this machine: download once, verify by SHA-256 every time."""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from aerochorus.sweep_contracts import ModelRead

log = logging.getLogger(__name__)
CHUNK = 8 * 1024 * 1024


class ModelArtifactError(RuntimeError):
    pass


class _AlreadyComplete(Exception):
    pass


@dataclass(frozen=True)
class Verification:
    path: Path
    present: bool
    size_ok: bool = False
    sha256: str | None = None

    @property
    def ok(self) -> bool:
        return self.present and self.size_ok and self.sha256 is not None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


class ModelStore:
    def __init__(self, models_dir: Path) -> None:
        self.models_dir = Path(models_dir)

    def path_for(self, model: ModelRead) -> Path:
        return self.models_dir / model.model_filename

    def verify(self, model: ModelRead) -> Verification:
        path = self.path_for(model)
        if not path.is_file():
            return Verification(path, present=False)
        size_ok = model.artifact_size_bytes is None or (
            path.stat().st_size == model.artifact_size_bytes
        )
        if not size_ok:
            return Verification(path, present=True, size_ok=False)
        digest = sha256_file(path)
        return Verification(
            path,
            present=True,
            size_ok=True,
            sha256=digest if digest == model.model_sha256 else None,
        )

    def require(self, model: ModelRead) -> Path:
        check = self.verify(model)
        if not check.present:
            raise ModelArtifactError(
                f"{model.model_filename} is not in {self.models_dir}; "
                f"run `aerochorus worker models pull {model.logical_name}`"
            )
        if not check.ok:
            raise ModelArtifactError(
                f"{check.path} does not match the registered size/sha256 of {model.logical_name}"
            )
        return check.path

    def pull(
        self,
        model: ModelRead,
        *,
        progress: Callable[[int, int | None], None] | None = None,
        http: httpx.Client | None = None,
    ) -> Path:
        """Download (resuming a partial download) and verify; never keeps a bad file."""
        if not model.artifact_uri or not model.model_sha256:
            raise ModelArtifactError(f"{model.logical_name} has no pinned artifact to download")
        existing = self.verify(model)
        if existing.ok:
            return existing.path

        self.models_dir.mkdir(parents=True, exist_ok=True)
        final = self.path_for(model)
        partial = final.with_name(final.name + ".part")
        digest = hashlib.sha256()
        offset = 0
        if partial.exists():
            with open(partial, "rb") as fh:
                while chunk := fh.read(CHUNK):
                    digest.update(chunk)
                    offset += len(chunk)

        complete = model.artifact_size_bytes is not None and offset == model.artifact_size_bytes
        client = http or httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60.0))
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        try:
            if complete:
                # A previous run finished downloading but stopped before verifying.
                raise _AlreadyComplete
            with client.stream("GET", model.artifact_uri, headers=headers) as response:
                if offset and response.status_code != 206:
                    # Server ignored the range: start over.
                    offset, digest = 0, hashlib.sha256()
                    mode = "wb"
                else:
                    mode = "ab" if offset else "wb"
                response.raise_for_status()
                total = model.artifact_size_bytes
                with open(partial, mode) as fh:
                    for chunk in response.iter_bytes(CHUNK):
                        fh.write(chunk)
                        digest.update(chunk)
                        offset += len(chunk)
                        if progress:
                            progress(offset, total)
        except _AlreadyComplete:
            pass
        finally:
            if http is None:
                client.close()

        if model.artifact_size_bytes is not None and offset != model.artifact_size_bytes:
            raise ModelArtifactError(
                f"{model.model_filename}: downloaded {offset} bytes, expected "
                f"{model.artifact_size_bytes}; rerun to resume"
            )
        if digest.hexdigest() != model.model_sha256:
            partial.unlink()
            raise ModelArtifactError(
                f"{model.model_filename}: sha256 {digest.hexdigest()} does not match the "
                f"registry ({model.model_sha256}); discarded"
            )
        os.replace(partial, final)
        log.info("verified %s (%s)", final, model.model_sha256)
        return final
