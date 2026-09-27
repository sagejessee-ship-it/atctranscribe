"""PostgreSQL-backed tests.

Set ``AEROCHORUS_TEST_DATABASE_URL`` (the Compose stack creates
``aerochorus_test``). The schema is rebuilt from migrations once per session
and tables are truncated after each test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from corpus_builder import build_bwi_corpus
from fastapi.testclient import TestClient
from integration_support import SOURCE_KEY
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from aerochorus.api.app import create_app
from aerochorus.contracts import SourceCreate
from aerochorus.corpus.config import FilesystemAdapterConfig
from aerochorus.db.base import Base
from aerochorus.db.migrate import upgrade
from aerochorus.settings import ControlPlaneSettings
from aerochorus.worker.client import ApiClient


def reset_schema(url: str) -> None:
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("AEROCHORUS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set AEROCHORUS_TEST_DATABASE_URL to run PostgreSQL tests")
    reset_schema(url)
    upgrade(url)
    return url


@pytest.fixture
def app(database_url):
    app = create_app(ControlPlaneSettings(database_url=database_url))
    yield app
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with app.state.engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    app.state.engine.dispose()


@pytest.fixture
def http(app):
    with TestClient(app) as client:
        yield client


@pytest.fixture
def api(http) -> ApiClient:
    return ApiClient(http=http)


@pytest.fixture
def db(app):
    with Session(app.state.engine) as session:
        yield session


@pytest.fixture
def source(api):
    return api.create_source(
        SourceCreate(
            logical_key=SOURCE_KEY,
            name="Home ATC archive",
            adapter_config=FilesystemAdapterConfig(
                filename_parser="rtlsdr_airband", filename_timezone="America/New_York"
            ),
        )
    )


@pytest.fixture
def corpus(tmp_path) -> tuple[Path, dict]:
    root = tmp_path / "bwi"
    return root, build_bwi_corpus(root)
