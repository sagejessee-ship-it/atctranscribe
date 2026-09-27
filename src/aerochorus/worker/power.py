"""Keep the host awake while work is active; allow normal sleep when idle."""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator

log = logging.getLogger(__name__)


@contextlib.contextmanager
def keep_awake(reason: str) -> Iterator[None]:
    """On macOS, hold a ``caffeinate -i`` assertion for the duration of the block."""
    caffeinate = shutil.which("caffeinate") if sys.platform == "darwin" else None
    if caffeinate is None:
        yield
        return
    proc = subprocess.Popen([caffeinate, "-i", "-w", str(os.getpid())])
    log.debug("preventing idle sleep: %s", reason)
    try:
        yield
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
