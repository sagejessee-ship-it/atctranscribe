"""Control-plane settings (API, migrations). Workers use ``aerochorus.worker.config``."""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ControlPlaneSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AEROCHORUS_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://aerochorus:aerochorus@127.0.0.1:5432/aerochorus"
    # A running scan with no batch for this long is marked abandoned when a new
    # scan for the same source starts.
    scan_stale_after_seconds: int = 1800
    # Order-aware similarity at or above which two hypotheses "near-match"
    # (review workbench). Similarity, never a probability; recorded per row.
    near_match_threshold: float = 0.8
    # On-demand ADS-B context (ADR-020). Optional OpenSky Trino credentials; absent or
    # empty means "not configured". They stay on the control plane, never in responses.
    opensky_username: str | None = None
    opensky_password: SecretStr | None = None
    # OpenSky REST API client (account page -> API client): OAuth2 client credentials.
    opensky_client_id: str | None = None
    opensky_client_secret: SecretStr | None = None
    # auto: Trino (historical state vectors) when access works, else REST tracks.
    adsb_provider: str = "auto"
    adsb_rest_max_tracks: int = 8
    opensky_timeout_s: float = 120.0
    adsb_window_before_s: int = 60
    adsb_window_after_s: int = 60
    adsb_radius_nm: float = 10.0


@lru_cache
def get_settings() -> ControlPlaneSettings:
    return ControlPlaneSettings()
