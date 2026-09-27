"""Control-plane settings (API, migrations). Workers use ``aerochorus.worker.config``."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class ControlPlaneSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AEROCHORUS_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://aerochorus:aerochorus@127.0.0.1:5432/aerochorus"
    # A running scan with no batch for this long is marked abandoned when a new
    # scan for the same source starts.
    scan_stale_after_seconds: int = 1800


@lru_cache
def get_settings() -> ControlPlaneSettings:
    return ControlPlaneSettings()
