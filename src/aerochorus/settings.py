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
    # auto/local: airport-area ADS-B imported per day from adsb.lol (ADR-024).
    # opensky: Trino (historical state vectors) when access works, else REST tracks.
    # trino / rest: only that OpenSky interface.
    adsb_provider: str = "auto"
    adsb_rest_max_tracks: int = 8
    opensky_timeout_s: float = 120.0
    adsb_window_before_s: int = 60
    adsb_window_after_s: int = 60
    adsb_radius_nm: float = 20.0
    # The area kept when a day is imported from adsb.lol (~20 MB per day at 25 nm).
    adsb_airport: str = "KBWI"
    adsb_import_radius_nm: float = 25.0
    # Model adjudication (ADR-022). The control plane plans, prices and records batches;
    # it never holds the OpenRouter key (the runner, where the audio lives, does).
    adjudication_model: str = "~google/gemini-pro-latest"
    adjudication_max_items: int = 500  # per batch
    adjudication_max_items_local: int = 5000  # per batch, self-hosted models (ADR-025)
    adjudication_max_batch_usd: float = 25.0  # the largest cap a batch may be created with
    adjudication_max_audio_s: float = 120.0  # longer segments are skipped, never sent
    # USD per million tokens when OpenRouter's public price list is unreachable.
    adjudication_price_prompt: float = 2.0
    adjudication_price_completion: float = 12.0
    adjudication_price_audio: float = 2.0
    openrouter_models_url: str = "https://openrouter.ai/api/v1/models"


class AdjudicatorSettings(BaseSettings):
    """The adjudication runner (`aerochorus adjudicate run`) on the host with the audio."""

    # Empty values (compose passes unset variables through as "") mean "not set".
    model_config = SettingsConfigDict(
        env_prefix="AEROCHORUS_", env_file=".env", extra="ignore", env_ignore_empty=True
    )

    openrouter_api_key: SecretStr | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    adjudication_timeout_s: float = 300.0
    # Self-hosted adjudicator (ADR-025): a profile from config/adjudicators.toml, and
    # per-host overrides of it. The model server's API key comes only from here.
    adjudicator_profile: str | None = None
    adjudicator_profiles: str | None = None  # another profiles file
    adjudicator_url: str | None = None
    adjudicator_api_key: SecretStr | None = None
    adjudicator_model: str | None = None
    adjudicator_max_tokens: int | None = None
    adjudicator_temperature: float | None = None
    adjudicator_timeout_s: float | None = None
    adjudicator_max_audio_inputs: int | None = None
    adjudicator_strip_thinking: bool | None = None


@lru_cache
def get_settings() -> ControlPlaneSettings:
    return ControlPlaneSettings()
