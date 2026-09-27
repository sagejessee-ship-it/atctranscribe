"""Raw CrispASR output, stored exactly once as zstd-compressed JSON (ADR-004).

URIs are logical: ``artifact://<store>/raw/<ab>/<uuid>.json.zst``. The store
name maps to a root directory in each machine's worker config, the same way
corpus sources do.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import zstandard

SCHEME = "artifact://"


@dataclass(frozen=True)
class StoredArtifact:
    uri: str
    sha256: str
    size_bytes: int


class ArtifactStore:
    def __init__(self, root: Path, store: str) -> None:
        self.root = Path(root)
        self.store = store

    def _relative(self, result_id: UUID) -> str:
        name = str(result_id)
        return f"raw/{name[:2]}/{name}.json.zst"

    def write(self, result_id: UUID, envelope: dict[str, Any]) -> StoredArtifact:
        relative = self._relative(result_id)
        path = self.root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(envelope, ensure_ascii=False, sort_keys=True).encode()
        blob = zstandard.ZstdCompressor(level=10).compress(payload)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, path)
        return StoredArtifact(
            uri=f"{SCHEME}{self.store}/{relative}",
            sha256=hashlib.sha256(blob).hexdigest(),
            size_bytes=len(blob),
        )

    def read(self, uri: str) -> dict[str, Any]:
        prefix = f"{SCHEME}{self.store}/"
        if not uri.startswith(prefix):
            raise ValueError(f"{uri} does not belong to artifact store {self.store!r}")
        path = self.root.joinpath(*uri[len(prefix) :].split("/"))
        with open(path, "rb") as fh:
            return json.loads(zstandard.ZstdDecompressor().decompress(fh.read()))
