"""A seeded, throwaway AeroChorus stack for browser end-to-end tests.

    python tests/e2e/stack.py --port 8765

* recreates the ``aerochorus_e2e`` database (AEROCHORUS_E2E_DATABASE_URL) from migrations;
* writes a small synthetic BWI corpus to a temp dir and scans it through the API;
* seeds model results with known agreement, and a KBWI airport profile;
* serves the review edge (built UI + /api + read-only /audio) and the control
  plane in one process: the edge proxies to the control plane in-process.

A second source, ``offline_archive``, is indexed but deliberately not mounted
on the edge, to exercise the "source audio unavailable" path.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

from corpus_builder import TONE_A, TONE_B, local, place  # noqa: E402

from aerochorus.api.agreement import refresh_agreement  # noqa: E402
from aerochorus.api.app import create_app  # noqa: E402
from aerochorus.contracts import SourceCreate  # noqa: E402
from aerochorus.corpus.config import FilesystemAdapterConfig  # noqa: E402
from aerochorus.db.migrate import upgrade  # noqa: E402
from aerochorus.db.models import (  # noqa: E402
    ArchitectureFamily,
    Model,
    ModelSuite,
    Segment,
    SweepRun,
    SweepRunModel,
    TranscriptionResult,
)
from aerochorus.edge.server import create_edge_app  # noqa: E402
from aerochorus.settings import ControlPlaneSettings  # noqa: E402
from aerochorus.worker.client import ApiClient  # noqa: E402
from aerochorus.worker.config import SourceMount, WorkerConfig  # noqa: E402
from aerochorus.worker.scanner import CorpusScanner  # noqa: E402

DEFAULT_URL = "postgresql+psycopg://aerochorus:aerochorus@127.0.0.1:5432/aerochorus_e2e"
LATER = datetime(2030, 1, 1, tzinfo=UTC).timestamp()

MODELS = {
    "canary-e2e": "canary",
    "canary-e2e-beam": "canary",
    "parakeet-e2e": "parakeet",
    "whisper-e2e": "whisper",
}

# file name -> {model: (status, text)}; local times are America/New_York (EDT).
CORPUS = {
    "BWI_TWR_20260908_080000_119400000.mp3": {
        "canary-e2e": ("success", "American 2669, runway three three left, line up and wait."),
        "parakeet-e2e": ("success", "american 2669 runway three three left line up and wait"),
        "whisper-e2e": ("success", "American 2669 runway three three left line up and wait"),
    },
    "BWI_TWR_20260908_080020_119400000.mp3": {
        "canary-e2e": (
            "success",
            "southwest four five six contact departure one one niner point four",
        ),
        "parakeet-e2e": (
            "success",
            "southwest four five six contact departure one one nine point four",
        ),
    },
    "BWI_TWR_20260908_080040_119400000.mp3": {
        "canary-e2e": ("success", "united nine cleared to land runway one zero"),
        "canary-e2e-beam": ("success", "united nine cleared to land runway one zero"),
        "parakeet-e2e": ("success", "jetblue nine is cleared to land"),
        "whisper-e2e": ("success", "thank you good day"),
    },
    "BWI_GND_20260908_080100_121900000.mp3": {
        "canary-e2e": (
            "success",
            "delta one two three taxi via alpha hold short runway one five right",
        ),
        "whisper-e2e": (
            "success",
            "Delta one two three, taxi via alpha, hold short runway one five right.",
        ),
    },
    "BWI_GND_20260908_080130_121900000.mp3": {
        "canary-e2e": ("error", None),
        "parakeet-e2e": ("abstained", ""),
        "whisper-e2e": ("success", "roger"),
    },
    "BWI_TWR_20260908_080200_119400000.mp3": {},
}
OFFLINE = {
    "BWI_TWR_20260908_090000_119400000.mp3": {
        "canary-e2e": ("success", "spirit five one seven wind calm runway one zero"),
        "parakeet-e2e": ("success", "spirit five one seven wind calm runway one zero"),
    },
}

KBWI = {
    "icao": "KBWI",
    "faa_id": "BWI",
    "iata": "BWI",
    "name": "Baltimore/Washington Intl Thurgood Marshall",
    "city": "Baltimore",
    "latitude": 39.1754,
    "longitude": -76.6683,
    "elevation_ft": 143.0,
    "timezone": "America/New_York",
    "runways": [
        {
            "pair": pair,
            "end_ident": end,
            "length_ft": length,
            "width_ft": 150,
            "true_alignment": None,
            "spoken": [spoken],
        }
        for pair, end, length, spoken in (
            ("10/28", "10", 10503, "runway one zero"),
            ("10/28", "28", 10503, "runway two eight"),
            ("15L/33R", "15L", 5000, "runway one five left"),
            ("15L/33R", "33R", 5000, "runway three three right"),
            ("15R/33L", "15R", 9501, "runway one five right"),
            ("15R/33L", "33L", 9501, "runway three three left"),
        )
    ],
    "frequencies": [
        {
            "service": "TWR",
            "frequency_hz": 119_400_000,
            "facility": "BWI",
            "call": "Baltimore Tower",
            "sectorization": "LCL/P",
            "spoken": ["one one niner point four"],
        },
        {
            "service": "GND",
            "frequency_hz": 121_900_000,
            "facility": "BWI",
            "call": "Baltimore Ground",
            "sectorization": "GND/P",
            "spoken": ["one two one point niner"],
        },
    ],
    "aliases": [
        {"alias": "KBWI", "kind": "icao"},
        {"alias": "BWI", "kind": "faa"},
        {"alias": "BWI", "kind": "station"},
        {"alias": "Baltimore Tower", "kind": "spoken"},
    ],
    "provenance": {"source": "e2e fixture (not FAA data)", "cycle": "fixture"},
}


def recreate_database(url: str) -> None:
    target = make_url(url)
    admin = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": target.database}
        )
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{target.database}"'))
    admin.dispose()
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS reference CASCADE"))
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    upgrade(url)


def build_corpus(root: Path, files: dict) -> None:
    for i, name in enumerate(files):
        wall = datetime.strptime("".join(name.split("_")[2:4]), "%Y%m%d%H%M%S")
        start = local(*wall.timetuple()[:6])  # America/New_York wall clock -> UTC
        place(root, f"2026/09/08/{name}", TONE_B if i % 2 else TONE_A, start_utc=start)


def seed(app, corpus_root: Path, offline_root: Path) -> None:
    with TestClient(app) as http:
        api = ApiClient(http=http)
        config_for = {"home_atc_archive": corpus_root, "offline_archive": offline_root}
        for key, name in (
            ("home_atc_archive", "E2E BWI archive"),
            ("offline_archive", "E2E offline"),
        ):
            api.create_source(
                SourceCreate(
                    logical_key=key,
                    name=name,
                    adapter_config=FilesystemAdapterConfig(
                        filename_parser="rtlsdr_airband", filename_timezone="America/New_York"
                    ),
                )
            )
            config = WorkerConfig(
                worker_name="e2e",
                api_url="http://testserver",
                sources={key: SourceMount(root=config_for[key])},
            )
            CorpusScanner(api, config, clock=lambda: LATER).scan(key)
        response = http.put("/api/v1/airports/KBWI", json=KBWI)
        response.raise_for_status()

    with Session(app.state.engine) as db:
        db.add_all(ArchitectureFamily(key=f, display_name=f) for f in sorted(set(MODELS.values())))
        db.flush()
        models = {
            name: Model(
                logical_name=name,
                architecture_family=family,
                crisp_backend="e2e",
                model_filename=f"{name}.gguf",
                enabled=True,
            )
            for name, family in MODELS.items()
        }
        db.add_all(models.values())
        suite = ModelSuite(name="e2e")
        db.add(suite)
        db.flush()
        run = SweepRun(
            suite_id=suite.id,
            status="completed",
            selection_definition={},
            effective_config={},
            config_sha256="0" * 64,
            segments_total=0,
        )
        db.add(run)
        db.flush()
        srms = {}
        for order, model in enumerate(models.values()):
            srms[model.logical_name] = SweepRunModel(
                run_id=run.id,
                model_id=model.id,
                execution_order=order,
                status="completed",
                segments_total=0,
            )
        db.add_all(srms.values())
        db.flush()
        ids = []
        for files in (CORPUS, OFFLINE):
            for name, hypotheses in files.items():
                segment_id = db.scalar(
                    text("SELECT id FROM segment WHERE relative_path = :p"),
                    {"p": f"2026/09/08/{name}"},
                )
                ids.append(segment_id)
                for model, (status, words) in hypotheses.items():
                    db.add(
                        TranscriptionResult(
                            id=uuid.uuid4(),
                            sweep_run_model_id=srms[model].id,
                            segment_id=segment_id,
                            status=status,
                            attempt=1,
                            text=words,
                            error_type="timeout" if status == "error" else None,
                        )
                    )
        db.flush()
        refresh_agreement(db, ids)
        db.commit()
        assert db.query(Segment).count() == len(CORPUS) + len(OFFLINE)


class FakeOpenSky:
    """Deterministic stand-in for the Trino provider (no network). Counts calls."""

    name = "fake-opensky"

    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, query):
        from aerochorus.context.opensky import COLUMNS, ProviderResult

        self.calls += 1
        t = query.segment_utc
        rows = [
            [t + 2, "a8b1c2", "AAL2669 ", 39.19, -76.67, 30.0, 35.0, 7.5, 330.0, 0.0, True, "2201", t],  # noqa: E501
            [t - 9, "a0f00d", "SWA456  ", 39.25, -76.72, 1219.2, 1250.0, 102.9, 150.0, 5.1, False, "4312", t],  # noqa: E501
        ]  # fmt: skip
        return ProviderResult(list(COLUMNS), rows, {"query_id": f"fake-{self.calls}"})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--static", type=Path, default=ROOT / "ui" / "dist")
    parser.add_argument(
        "--fake-adsb", action="store_true", help="serve a deterministic fake OpenSky provider"
    )
    args = parser.parse_args()
    import os

    url = args.database_url or os.environ.get("AEROCHORUS_E2E_DATABASE_URL", DEFAULT_URL)
    recreate_database(url)
    work = Path(tempfile.mkdtemp(prefix="aerochorus-e2e-"))
    corpus_root, offline_root = work / "bwi", work / "offline"
    build_corpus(corpus_root, CORPUS)
    build_corpus(offline_root, OFFLINE)

    control = create_app(ControlPlaneSettings(database_url=url))
    control.state.adsb_provider = None  # never the real OpenSky from a test stack
    if args.fake_adsb:
        fake = FakeOpenSky()
        control.state.adsb_provider = fake
        # Test-only: lets the browser suite prove that cached snapshots are reused.
        control.add_api_route("/api/__e2e/adsb-calls", lambda: {"calls": fake.calls})
    seed(control, corpus_root, offline_root)

    upstream = httpx.AsyncClient(transport=httpx.ASGITransport(app=control), base_url="http://cp")
    edge = create_edge_app(
        # offline_archive is indexed but not mounted here, on purpose.
        WorkerConfig(
            worker_name="e2e-edge", sources={"home_atc_archive": SourceMount(root=corpus_root)}
        ),
        api_url="http://cp",
        static_dir=args.static,
        upstream=upstream,
    )
    database = make_url(url).database
    print(f"e2e stack: http://127.0.0.1:{args.port}/review (db {database}, corpus {work})")
    uvicorn.run(edge, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
