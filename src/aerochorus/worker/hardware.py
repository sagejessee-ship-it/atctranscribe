"""Hardware inventory and runtime profile for a worker host (ADR-021).

A *hardware profile* (e.g. ``linux_pascal_8gb``) names a class of machine on
which model qualification is valid. It is derived from what is detected (OS,
GPU architecture, VRAM) or set explicitly in the worker config; it is never a
hostname. Qualification results are recorded per (model, profile).

Everything here only reads the system; nothing is installed or changed.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

# NVIDIA compute capability -> architecture name.
ARCHITECTURES = (
    (12.0, "blackwell"),
    (10.0, "blackwell"),
    (9.0, "hopper"),
    (8.9, "ada"),
    (8.0, "ampere"),
    (7.5, "turing"),
    (7.0, "volta"),
    (6.0, "pascal"),
    (5.0, "maxwell"),
    (3.0, "kepler"),
)
# CUDA 13 dropped code generation for GPUs older than Turing (sm_75).
CUDA13_MIN_CC = 7.5


@dataclass(frozen=True)
class GpuInfo:
    index: int
    name: str
    compute_capability: float | None
    memory_total_mb: int | None
    driver_version: str | None

    @property
    def architecture(self) -> str:
        return architecture(self.compute_capability)

    @property
    def cuda13_supported(self) -> bool:
        return self.compute_capability is not None and self.compute_capability >= CUDA13_MIN_CC


@dataclass
class Inventory:
    os: str
    os_pretty: str
    kernel: str
    machine: str
    cpu_model: str
    cpu_cores: int | None
    ram_total_mb: int | None
    gpus: list[GpuInfo] = field(default_factory=list)
    cuda_driver_version: str | None = None  # max CUDA the driver supports ("12.4")
    disks: dict[str, dict[str, int]] = field(default_factory=dict)
    python: str = sys.version.split()[0]

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["gpus"] = [asdict(g) | {"architecture": g.architecture} for g in self.gpus]
        return data


def architecture(compute_capability: float | None) -> str:
    if compute_capability is None:
        return "unknown"
    for floor, name in ARCHITECTURES:
        if compute_capability >= floor:
            return name
    return "legacy"


def _run(args: list[str], timeout: float = 15) -> str | None:
    if shutil.which(args[0]) is None:
        return None
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def parse_nvidia_smi_csv(text: str) -> list[GpuInfo]:
    """Parse ``nvidia-smi --query-gpu=index,name,compute_cap,memory.total,driver_version``
    with ``--format=csv,noheader,nounits``."""
    gpus = []
    for line in text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        index, name, cc, memory, driver = parts[:5]
        try:
            capability = float(cc)
        except ValueError:
            capability = None
        try:
            memory_mb = int(float(memory))
        except ValueError:
            memory_mb = None
        gpus.append(GpuInfo(int(index), name, capability, memory_mb, driver or None))
    return gpus


def parse_cuda_driver_version(text: str) -> str | None:
    # "CUDA Version: 12.2" (older drivers) or "CUDA UMD Version: 13.3" (newer).
    match = re.search(r"CUDA (?:UMD )?Version:\s*([\d.]+)", text or "")
    return match[1] if match else None


def detect_gpus() -> tuple[list[GpuInfo], str | None]:
    csv = _run(
        [
            "nvidia-smi",
            "--query-gpu=index,name,compute_cap,memory.total,driver_version",
            "--format=csv,noheader,nounits",
        ]
    )
    gpus = parse_nvidia_smi_csv(csv) if csv else []
    return gpus, parse_cuda_driver_version(_run(["nvidia-smi"]) or "")


def gpu_memory_used_mb(index: int = 0) -> int | None:
    out = _run(
        [
            "nvidia-smi",
            f"--id={index}",
            "--query-gpu=memory.used",
            "--format=csv,noheader,nounits",
        ],
        timeout=10,
    )
    try:
        return int(float(out.strip())) if out else None
    except ValueError:
        return None


def parse_meminfo(text: str) -> int | None:
    match = re.search(r"^MemTotal:\s+(\d+)\s+kB", text, re.MULTILINE)
    return int(match[1]) // 1024 if match else None


def _ram_total_mb() -> int | None:
    if sys.platform.startswith("linux"):
        try:
            return parse_meminfo(Path("/proc/meminfo").read_text())
        except OSError:
            return None
    if sys.platform == "darwin":
        out = _run(["sysctl", "-n", "hw.memsize"])
        return int(out) // (1024 * 1024) if out and out.strip().isdigit() else None
    if sys.platform == "win32":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
            return int(status.ullTotalPhys // (1024 * 1024))
    return None


def _cpu_model() -> str:
    if sys.platform.startswith("linux"):
        try:
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.processor() or platform.machine()


def _os_pretty() -> str:
    try:
        text = Path("/etc/os-release").read_text()
        match = re.search(r'^PRETTY_NAME="?([^"\n]+)"?', text, re.MULTILINE)
        if match:
            return match[1]
    except OSError:
        pass
    return platform.platform()


def _disks(paths: list[Path]) -> dict[str, dict[str, int]]:
    out = {}
    for path in paths:
        probe = path
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            usage = shutil.disk_usage(probe)
        except OSError:
            continue
        out[str(path)] = {"total_gb": usage.total // 10**9, "free_gb": usage.free // 10**9}
    return out


def collect_inventory(data_paths: list[Path] | None = None) -> Inventory:
    gpus, cuda = detect_gpus()
    return Inventory(
        os=platform.system().lower(),
        os_pretty=_os_pretty(),
        kernel=platform.release(),
        machine=platform.machine(),
        cpu_model=_cpu_model(),
        cpu_cores=os.cpu_count(),
        ram_total_mb=_ram_total_mb(),
        gpus=gpus,
        cuda_driver_version=cuda,
        disks=_disks(data_paths or []),
    )


def suggest_profile(inventory: Inventory) -> str:
    """``<os>_<gpu architecture>_<vram GB>gb``, or ``<os>_cpu_<ram GB>gb`` without a GPU."""
    system = {"darwin": "macos"}.get(inventory.os, inventory.os)
    if inventory.gpus:
        gpu = inventory.gpus[0]
        vram_gb = round((gpu.memory_total_mb or 0) / 1024)
        return f"{system}_{gpu.architecture}_{vram_gb}gb"
    ram_gb = round((inventory.ram_total_mb or 0) / 1024)
    return f"{system}_cpu_{ram_gb}gb"


def cuda_compatibility(inventory: Inventory) -> dict[str, Any]:
    """Which CrispASR CUDA build this host needs (Pascal => CUDA 12.x only)."""
    if not inventory.gpus:
        return {"cuda": False, "reason": "no NVIDIA GPU detected (nvidia-smi)"}
    gpu = inventory.gpus[0]
    return {
        "cuda": True,
        "gpu": gpu.name,
        "compute_capability": gpu.compute_capability,
        "architecture": gpu.architecture,
        "cuda13_supported": gpu.cuda13_supported,
        "required_crispasr_build": "cuda13 or cuda12" if gpu.cuda13_supported else "cuda12",
        "driver_max_cuda": inventory.cuda_driver_version,
    }


@lru_cache(maxsize=1)
def _detected_profile() -> str:
    return suggest_profile(collect_inventory())


def resolve_profile(configured: str | None) -> str:
    """The configured profile, else one derived from detected hardware (cached)."""
    return configured or _detected_profile()
