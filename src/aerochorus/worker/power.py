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

# Windows SetThreadExecutionState flags.
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


@contextlib.contextmanager
def keep_awake(reason: str) -> Iterator[None]:
    """Prevent idle sleep for the duration of the block.

    macOS: a ``caffeinate -i`` assertion. Windows: ``SetThreadExecutionState``
    on this thread (the display may still turn off). Linux servers do not sleep
    on their own, so nothing is needed there.
    """
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        log.debug("preventing idle sleep: %s", reason)
        try:
            yield
        finally:
            kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        return
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
