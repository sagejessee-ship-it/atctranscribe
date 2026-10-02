"""Review edge server (ADR-016).

Runs natively on a machine that has the corpus mounted (the Mac in
production), next to the worker, with the worker's config. It serves:

* the built review UI (``ui/dist``) with SPA fallback;
* ``/api/*``, proxied to the control plane, so the browser has one origin;
* ``/audio/{segment_id}``: source audio, read through ``ReadOnlyCorpusReader``,
  checked against the indexed sha256, with HTTP Range support for seeking;
* ``/edge/test-pack``: a zip of selected segments' audio with a chat-ready
  adjudication prompt each, for small manual experiments (built in memory).

Like the worker it never imports the database layer; the control plane stays
the only database writer, and the container never needs the corpus mounted.
Audio bytes are never modified, transcoded or cached on disk.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import os
import re
import zipfile
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from aerochorus import __version__
from aerochorus.worker.config import WorkerConfig
from aerochorus.worker.fs import ReadOnlyCorpusReader

log = logging.getLogger(__name__)

AUDIO_TYPES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
}
# Headers that must not be forwarded by a proxy (RFC 9110 §7.6.1) plus ones httpx recomputes.
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "content-encoding",
}
RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")
TEST_PACK_MAX = 200

PACK_README = """\
AeroChorus test pack: {count} segment(s), made {made} UTC.

For each segment there are three files:
  NNN_<id>_<original name>.mp3   the source audio, byte for byte (sha256 checked)
  NNN_<id>_prompt.txt            a short prompt for a chat UI: paste it, attach the audio
  NNN_<id>_full-prompt.txt       the instructions and context exactly as AeroChorus's
                                 automated adjudicator sends them (prompt version {version})

The prompts include each segment's machine transcripts, the airport, and the ADS-B
traffic if it was fetched for that segment. manifest.csv lists every segment.
"""


class TestPackRequest(BaseModel):
    segment_ids: list[int] = Field(min_length=1, max_length=TEST_PACK_MAX)


def _zip(entries: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as pack:
        for name, data in entries:
            # Audio is already compressed: store it; deflate the text.
            kind = zipfile.ZIP_DEFLATED if name.endswith((".txt", ".csv")) else zipfile.ZIP_STORED
            pack.writestr(name, data, compress_type=kind)
    return buffer.getvalue()


def default_static_dir() -> Path | None:
    if env := os.environ.get("AEROCHORUS_UI_DIST"):
        return Path(env)
    repo_dist = Path(__file__).resolve().parents[3] / "ui" / "dist"
    return repo_dist if repo_dist.is_dir() else None


class AudioCache:
    """Small in-memory LRU of verified segment audio (segments are ~100 KB)."""

    def __init__(self, budget_bytes: int = 64 * 1024 * 1024) -> None:
        self.budget = budget_bytes
        self.used = 0
        self._items: OrderedDict[int, tuple[bytes, str]] = OrderedDict()

    def get(self, segment_id: int) -> tuple[bytes, str] | None:
        item = self._items.get(segment_id)
        if item is not None:
            self._items.move_to_end(segment_id)
        return item

    def put(self, segment_id: int, data: bytes, digest: str) -> None:
        if len(data) > self.budget:
            return
        if segment_id in self._items:
            self.used -= len(self._items.pop(segment_id)[0])
        self._items[segment_id] = (data, digest)
        self.used += len(data)
        while self.used > self.budget:
            _, (old, _) = self._items.popitem(last=False)
            self.used -= len(old)


def _byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Parse a single ``bytes=a-b`` range into an inclusive (start, end); None = whole file."""
    if not header:
        return None
    match = RANGE.match(header.strip())
    if not match or (not match.group(1) and not match.group(2)):
        raise HTTPException(416, "only single byte ranges are supported")
    first, last = match.groups()
    if first:
        start, end = int(first), int(last) if last else size - 1
    else:  # suffix range: the last N bytes
        start, end = max(size - int(last), 0), size - 1
    if start >= size or start > end:
        raise HTTPException(416, f"range not satisfiable for {size} bytes")
    return start, min(end, size - 1)


def create_edge_app(
    config: WorkerConfig,
    *,
    api_url: str | None = None,
    static_dir: Path | None = None,
    upstream: httpx.AsyncClient | None = None,
) -> FastAPI:
    api_url = api_url or config.api_url
    client = upstream or httpx.AsyncClient(base_url=api_url, timeout=120)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await client.aclose()

    app = FastAPI(title="AeroChorus review edge", version=__version__, lifespan=lifespan)
    readers = {key: ReadOnlyCorpusReader(mount.root) for key, mount in config.sources.items()}
    cache = AudioCache()
    static_dir = static_dir if static_dir is not None else default_static_dir()

    @app.get("/edge/health")
    async def health() -> dict:
        sources = {}
        for key, reader in readers.items():
            availability = await run_in_threadpool(reader.check_available)
            sources[key] = {"available": availability.ok, "reason": availability.reason}
        try:
            upstream_ok = (await client.get("/health")).status_code == 200
        except httpx.TransportError:
            upstream_ok = False
        return {
            "version": __version__,
            "api_url": api_url,
            "api_reachable": upstream_ok,
            "sources": sources,
            "ui": str(static_dir) if static_dir else None,
        }

    async def _segment(segment_id: int) -> dict:
        try:
            response = await client.get(f"/api/v1/segments/{segment_id}")
        except httpx.TransportError as exc:
            raise HTTPException(502, f"control plane unreachable at {api_url}: {exc}") from exc
        if response.status_code == 404:
            raise HTTPException(404, f"segment {segment_id} not found")
        response.raise_for_status()
        return response.json()

    async def _load(segment_id: int) -> tuple[bytes, str, str]:
        segment = await _segment(segment_id)
        media_type = AUDIO_TYPES.get(Path(segment["relative_path"]).suffix.lower(), "audio/mpeg")
        if (cached := cache.get(segment_id)) is not None:
            return cached[0], cached[1], media_type
        key = segment["source_key"]
        reader = readers.get(key)
        if reader is None:
            raise HTTPException(
                503,
                f"source audio unavailable: source {key!r} is not mounted on this machine "
                "(add it under [sources] in the worker config used by `aerochorus ui serve`)",
            )
        availability = await run_in_threadpool(reader.check_available)
        if not availability.ok:
            raise HTTPException(503, f"source audio unavailable: {availability.reason}")
        try:
            data = await run_in_threadpool(reader.read_bytes, segment["relative_path"])
        except FileNotFoundError as exc:
            raise HTTPException(
                404, f"source file missing: {key}/{segment['relative_path']}"
            ) from exc
        except OSError as exc:
            raise HTTPException(503, f"source audio unreadable: {exc}") from exc
        digest = hashlib.sha256(data).hexdigest()
        if segment.get("sha256") and digest != segment["sha256"]:
            raise HTTPException(
                409,
                f"source audio changed since it was indexed (sha256 {digest[:12]}… != "
                f"{segment['sha256'][:12]}…); rescan the source before reviewing it",
            )
        cache.put(segment_id, data, digest)
        return data, digest, media_type

    @app.get("/audio/{segment_id}")
    async def audio(segment_id: int, request: Request) -> Response:
        data, digest, media_type = await _load(segment_id)
        size = len(data)
        headers = {
            "Accept-Ranges": "bytes",
            "ETag": f'"{digest}"',
            "Cache-Control": "private, max-age=86400, immutable",
            "X-Audio-Sha256": digest,
        }
        span = _byte_range(request.headers.get("range"), size)
        if span is None:
            return Response(data, media_type=media_type, headers=headers)
        start, end = span
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return Response(
            data[start : end + 1], status_code=206, media_type=media_type, headers=headers
        )

    @app.post("/edge/test-pack")
    async def test_pack(body: TestPackRequest) -> Response:
        """Selected segments' audio + adjudication prompts as one zip (manual experiments)."""
        ids = list(dict.fromkeys(body.segment_ids))
        entries: list[tuple[str, bytes]] = []
        rows: list[list[str]] = []
        skipped: list[str] = []
        version = "?"
        for n, segment_id in enumerate(ids, 1):
            try:
                data, digest, _ = await _load(segment_id)
            except HTTPException as exc:
                skipped.append(f"segment {segment_id}: {exc.detail}")
                continue
            try:
                response = await client.get(f"/api/v1/segments/{segment_id}/adjudication-context")
            except httpx.TransportError as exc:
                raise HTTPException(502, f"control plane unreachable at {api_url}: {exc}") from exc
            if response.status_code != 200:
                skipped.append(f"segment {segment_id}: no prompt context ({response.status_code})")
                continue
            ctx = response.json()
            version = ctx["prompt_version"]
            stem = f"{n:03d}_{segment_id}"
            audio_name = f"{stem}_{PurePosixPath(ctx['relative_path']).name}"
            prompt = f"[Attach {audio_name}]\n\n{ctx['chat_prompt']}\n"
            full = f"{ctx['system_prompt']}\n\n----- context -----\n\n{ctx['context_text']}\n"
            entries += [
                (audio_name, data),
                (f"{stem}_prompt.txt", prompt.encode()),
                (f"{stem}_full-prompt.txt", full.encode()),
            ]
            rows.append([str(segment_id), audio_name, ctx["relative_path"], digest])
        if not rows:
            raise HTTPException(
                503, "none of the selected segments could be packed: " + "; ".join(skipped)
            )
        made = datetime.now(UTC)
        manifest = io.StringIO()
        writer = csv.writer(manifest, lineterminator="\n")
        writer.writerow(["segment_id", "audio_file", "source_path", "audio_sha256"])
        writer.writerows(rows)
        readme = PACK_README.format(
            count=len(rows), made=made.strftime("%Y-%m-%d %H:%M"), version=version
        )
        if skipped:
            readme += "\nNot included:\n" + "\n".join(f"  {line}" for line in skipped) + "\n"
        entries += [("README.txt", readme.encode()), ("manifest.csv", manifest.getvalue().encode())]
        content = await run_in_threadpool(_zip, entries)
        name = f"aerochorus-test-pack-{len(rows)}-segments-{made.strftime('%Y%m%d-%H%M%S')}.zip"
        return Response(
            content,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                "X-Pack-Segments": str(len(rows)),
                "X-Pack-Skipped": str(len(skipped)),
                "Access-Control-Expose-Headers": (
                    "Content-Disposition, X-Pack-Segments, X-Pack-Skipped"
                ),
            },
        )

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def proxy(path: str, request: Request) -> Response:
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
        try:
            response = await client.request(
                request.method,
                f"/api/{path}",
                params=request.query_params,
                content=await request.body(),
                headers=headers,
            )
        except httpx.TransportError as exc:
            return JSONResponse(
                {"detail": f"control plane unreachable at {api_url}: {type(exc).__name__}"},
                status_code=502,
            )
        passthrough = {k: v for k, v in response.headers.items() if k.lower() not in HOP_BY_HOP}
        return Response(response.content, status_code=response.status_code, headers=passthrough)

    @app.get("/{path:path}", include_in_schema=False)
    async def ui(path: str) -> Response:
        if static_dir is None or not (static_dir / "index.html").is_file():
            return PlainTextResponse(
                "AeroChorus review UI is not built. Run `npm ci && npm run build` in ui/, "
                "or use the Vite dev server (`npm run dev`).",
                status_code=503,
            )
        root = static_dir.resolve()
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(root / "index.html", headers={"Cache-Control": "no-cache"})

    return app
