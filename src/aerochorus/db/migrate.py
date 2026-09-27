"""Programmatic Alembic access; migrations ship inside the package."""

from __future__ import annotations

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine


def alembic_config(database_url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", "aerochorus:migrations")
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def upgrade(database_url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url), revision)


def downgrade(database_url: str, revision: str) -> None:
    command.downgrade(alembic_config(database_url), revision)


def head_revision() -> str | None:
    script = ScriptDirectory.from_config(alembic_config("postgresql://unused"))
    return script.get_current_head()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()
