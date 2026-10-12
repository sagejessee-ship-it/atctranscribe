"""Start vLLM for one adjudicator profile (ADR-025): ``serve.py <profile>``.

Reads ``[profiles.<name>.vllm]`` from the adjudicator profiles file (the one the
runner reads too) and execs::

    vllm serve <hf model> --served-model-name <profile model> \
        --host 0.0.0.0 --port 8000 <profile args> <AEROCHORUS_VLLM_EXTRA_ARGS>

so the served name is the id batches are recorded under. Standard library only: it
runs inside the vLLM image. ``--dry-run`` prints the command instead. A key, if the
server should require one, comes from VLLM_API_KEY (vLLM reads it itself).
"""

from __future__ import annotations

import os
import shlex
import sys
import tomllib
from pathlib import Path

DEFAULT_PROFILES = "/config/adjudicators.toml"


def command(profiles_path: str | Path, name: str, *, port: int = 8000) -> list[str]:
    try:
        raw = tomllib.loads(Path(profiles_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(
            f"profiles file not found: {profiles_path} (set AEROCHORUS_ADJUDICATOR_PROFILES)"
        ) from None
    profiles = raw.get("profiles") or {}
    if name not in profiles:
        raise SystemExit(f"unknown profile {name!r} (known: {', '.join(sorted(profiles))})")
    profile = profiles[name]
    serve = profile.get("vllm")
    if not serve:
        raise SystemExit(f"profile {name} has no [profiles.{name}.vllm] table: not self-hosted")
    return [
        "vllm", "serve", serve["model"],
        "--served-model-name", profile["model"],
        "--host", "0.0.0.0", "--port", str(port),
        *serve.get("args", []),
        *shlex.split(os.environ.get("AEROCHORUS_VLLM_EXTRA_ARGS", "")),
    ]  # fmt: skip


def main(argv: list[str]) -> None:
    args = [a for a in argv[1:] if a != "--dry-run"]
    name = args[0] if args else os.environ.get("AEROCHORUS_ADJUDICATOR_PROFILE")
    if not name:
        raise SystemExit("usage: serve.py <profile> [--dry-run]")
    path = os.environ.get("AEROCHORUS_ADJUDICATOR_PROFILES", DEFAULT_PROFILES)
    cmd = command(path, name)
    print("adjudicator profile", name, "->", shlex.join(cmd), flush=True)
    if "--dry-run" not in argv:
        os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main(sys.argv)
