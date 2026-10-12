"""Adjudicator profiles (ADR-025): which model adjudicates, and how its server starts.

A profile is one table in ``config/adjudicators.toml``. It names a backend type, the
model id batches and results are recorded under, where the model server is, the
request settings and, for a self-hosted model, the ``vllm serve`` arguments. Moving
from the 16 GB development GPU to an H100 host is a change of profile, not of code.

Choose one with ``AEROCHORUS_ADJUDICATOR_PROFILE`` (or ``--profile``). Any
``AEROCHORUS_ADJUDICATOR_<FIELD>`` setting overrides that field on one host (url,
model, max_tokens, temperature, timeout_s, max_audio_inputs, strip_thinking). API keys
never live in the file: ``AEROCHORUS_ADJUDICATOR_API_KEY``.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aerochorus.adjudication_contracts import LOCAL_MODEL_PREFIX
from aerochorus.settings import AdjudicatorSettings

DEFAULT_PROFILES = Path("config/adjudicators.toml")
# An editable checkout run from another directory still finds the repository's file.
_REPO_PROFILES = Path(__file__).resolve().parents[3] / "config" / "adjudicators.toml"
OVERRIDABLE = (
    "url",
    "model",
    "max_tokens",
    "temperature",
    "timeout_s",
    "max_audio_inputs",
    "strip_thinking",
)


class VllmServe(BaseModel):
    """How the model server starts (``deploy/vllm/serve.py`` reads the same table)."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model: str  # Hugging Face repo id
    args: list[str] = Field(default_factory=list)
    gpus: int = Field(default=1, ge=1)
    vram_gb: float | None = None  # expected use per GPU, for docs and the pre-flight check


class GateConfig(BaseModel):
    """Accept an adjudicated transcript without a paid second opinion when enough
    independent ASR families say the same thing (``aerochorus.adjudication.gate``)."""

    model_config = ConfigDict(extra="forbid")

    min_families: int = Field(default=2, ge=1)
    min_confidence: float = Field(default=0.0, ge=0, le=1)
    # 1.0: identical after normalisation. Lower: order-aware similarity at least this.
    min_similarity: float = Field(default=1.0, gt=0, le=1)
    include_research_only: bool = False  # research-only models never vote by default


class AdjudicatorProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    name: str
    description: str = ""
    # One class per kind of API (aerochorus.adjudication.backends.BACKENDS).
    backend: Literal["openai_compatible"] = "openai_compatible"
    # The model id batches are recorded under. Self-hosted: "local/<name>", which is
    # also the server's --served-model-name.
    model: str
    url: str  # the OpenAI-compatible base URL, e.g. http://gpu-host:8100/v1
    max_tokens: int = Field(default=1024, ge=64)
    temperature: float | None = 0.0
    strip_thinking: bool = True
    # 1: the whole clip only. More: the whole clip plus its squelch-split transmissions.
    max_audio_inputs: int = Field(default=1, ge=1, le=16)
    timeout_s: float = Field(default=120.0, gt=0)
    # Where the instructions go: "user" for models without a system role (Voxtral).
    system_prompt: Literal["system", "user"] = "system"
    structured_output: bool = True  # response_format json_schema (guided decoding)
    parse_retries: int = Field(default=1, ge=0, le=3)  # with a stricter reminder
    concurrency: int = Field(default=2, ge=1, le=8)
    extra_body: dict[str, Any] = Field(default_factory=dict)  # merged into each request
    vllm: VllmServe | None = None

    @model_validator(mode="after")
    def _self_hosted_is_local(self) -> AdjudicatorProfile:
        if self.vllm is not None and not self.model.startswith(LOCAL_MODEL_PREFIX):
            raise ValueError(
                f"profile {self.name}: a self-hosted model id must start with "
                f"{LOCAL_MODEL_PREFIX!r} (it is free and claimed only by its own runner)"
            )
        return self


class ProfileBook(BaseModel):
    gate: GateConfig = Field(default_factory=GateConfig)
    profiles: dict[str, AdjudicatorProfile]


def profiles_path(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit)
    return DEFAULT_PROFILES if DEFAULT_PROFILES.exists() else _REPO_PROFILES


def load_profiles(path: str | Path | None = None) -> ProfileBook:
    source = profiles_path(path)
    raw = tomllib.loads(source.read_text(encoding="utf-8"))
    return ProfileBook(
        gate=GateConfig(**(raw.get("gate") or {})),
        profiles={
            name: AdjudicatorProfile(name=name, **body)
            for name, body in (raw.get("profiles") or {}).items()
        },
    )


def resolve_profile(
    settings: AdjudicatorSettings, name: str | None = None
) -> tuple[AdjudicatorProfile, GateConfig]:
    """The chosen profile with this host's overrides applied, and the gate settings."""
    name = name or settings.adjudicator_profile
    if not name:
        raise LookupError(
            "no adjudicator profile: pass --profile or set AEROCHORUS_ADJUDICATOR_PROFILE"
        )
    book = load_profiles(settings.adjudicator_profiles)
    if name not in book.profiles:
        known = ", ".join(sorted(book.profiles)) or "none"
        raise LookupError(f"unknown adjudicator profile {name!r} (known: {known})")
    overrides = {
        field: value
        for field in OVERRIDABLE
        if (value := getattr(settings, f"adjudicator_{field}")) is not None
    }
    profile = AdjudicatorProfile.model_validate(book.profiles[name].model_dump() | overrides)
    return profile, book.gate
