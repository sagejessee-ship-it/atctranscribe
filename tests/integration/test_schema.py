from datetime import UTC, datetime

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from aerochorus.db.base import Base
from aerochorus.db.migrate import downgrade, upgrade

NOW = datetime(2026, 9, 26, tzinfo=UTC)


def test_models_match_migrations(database_url):
    engine = create_engine(database_url)
    with engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(context, Base.metadata)
    engine.dispose()
    assert diff == []


def test_migrations_round_trip(database_url):
    downgrade(database_url, "base")
    upgrade(database_url)
    engine = create_engine(database_url)
    with engine.connect() as conn:
        tables = set(
            conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )
    engine.dispose()
    assert {"corpus_source", "segment", "model", "architecture_family"} <= tables


def _insert_source(conn, read_only=True):
    return conn.execute(
        text(
            "INSERT INTO corpus_source (logical_key, name, read_only, adapter_type)"
            " VALUES ('src', 'Source', :ro, 'filesystem') RETURNING id"
        ),
        {"ro": read_only},
    ).scalar_one()


def _insert_segment(conn, source_id, **overrides):
    values = {
        "source_id": source_id,
        "relative_path": "2026/09/08/a.mp3",
        "relative_dir": "2026/09/08",
        "temporal_status": "unresolved",
        "capture_start_utc": None,
        "file_size": 10,
        "file_mtime": NOW,
        "file_mtime_ns": 1,
        "first_seen_at": NOW,
        "last_seen_at": NOW,
    } | overrides
    columns = ", ".join(values)
    params = ", ".join(f":{k}" for k in values)
    conn.execute(text(f"INSERT INTO segment ({columns}) VALUES ({params})"), values)


def test_sources_must_be_read_only(app):
    with app.state.engine.connect() as conn, pytest.raises(IntegrityError, match="read_only"):
        _insert_source(conn, read_only=False)


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"capture_start_utc": NOW}, "utc_matches_temporal_status"),
        ({"temporal_status": "resolved"}, "utc_matches_temporal_status"),
        ({"relative_path": "2026/../a.mp3", "relative_dir": "2026/.."}, "relative_path_format"),
        ({"relative_path": "2026\\a.mp3", "relative_dir": ""}, "relative_path_format"),
        ({"relative_dir": "2026/09"}, "relative_dir_is_parent"),
        ({"sha256": "ABC"}, "sha256_format"),
        ({"temporal_status": "guessed"}, "temporal_status"),
    ],
)
def test_segment_invariants(app, overrides, constraint):
    with app.state.engine.connect() as conn:
        source_id = _insert_source(conn)
        with pytest.raises(IntegrityError, match=constraint):
            _insert_segment(conn, source_id, **overrides)


def test_segment_identity_is_unique(app):
    with app.state.engine.connect() as conn:
        source_id = _insert_source(conn)
        _insert_segment(conn, source_id)
        with pytest.raises(IntegrityError, match="uq_segment_source_id_relative_path"):
            _insert_segment(conn, source_id)


def test_model_registry_rules(app):
    with app.state.engine.connect() as conn:
        with pytest.raises(IntegrityError, match="fk_model_architecture_family"):
            conn.execute(
                text(
                    "INSERT INTO model (logical_name, architecture_family, crisp_backend,"
                    " model_filename) VALUES ('parakeet-tdt-0.6b-v3', 'parakeet', 'parakeet',"
                    " 'parakeet.gguf')"
                )
            )
        conn.rollback()
        conn.execute(
            text("INSERT INTO architecture_family (key, display_name) VALUES ('parakeet', 'P')")
        )
        with pytest.raises(IntegrityError, match="sweep_requires_sha256"):
            conn.execute(
                text(
                    "INSERT INTO model (logical_name, architecture_family, crisp_backend,"
                    " model_filename, sweep_eligible) VALUES ('p', 'parakeet', 'parakeet',"
                    " 'p.gguf', true)"
                )
            )
