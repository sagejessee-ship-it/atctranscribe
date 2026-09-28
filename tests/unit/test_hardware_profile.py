"""ADR-021: hardware inventory parsing, profile ids, CUDA 12 vs 13, qualification states."""

from __future__ import annotations

import pytest

from aerochorus.sweep_contracts import PlatformState
from aerochorus.worker.config import CrispAsrConfig, ModelRuntime
from aerochorus.worker.crispasr import classify_log, effective_memory
from aerochorus.worker.hardware import (
    GpuInfo,
    Inventory,
    architecture,
    cuda_compatibility,
    parse_cuda_driver_version,
    parse_meminfo,
    parse_nvidia_smi_csv,
    suggest_profile,
)
from aerochorus.worker.qualify import Measurement, Thresholds, classify


def inventory(*gpus: GpuInfo, os: str = "linux", ram_mb: int = 32_000) -> Inventory:
    return Inventory(
        os=os, os_pretty="Ubuntu 24.04", kernel="6.8", machine="x86_64",
        cpu_model="i7", cpu_cores=8, ram_total_mb=ram_mb, gpus=list(gpus),
    )  # fmt: skip


GTX1070 = parse_nvidia_smi_csv("0, NVIDIA GeForce GTX 1070, 6.1, 8192, 550.120\n")[0]
RTX5080 = parse_nvidia_smi_csv("0, NVIDIA GeForce RTX 5080, 12.0, 16303, 610.88\n")[0]


def test_nvidia_smi_parsing():
    assert GpuInfo(0, "NVIDIA GeForce GTX 1070", 6.1, 8192, "550.120") == GTX1070
    assert GTX1070.architecture == "pascal" and not GTX1070.cuda13_supported
    assert RTX5080.architecture == "blackwell" and RTX5080.cuda13_supported
    assert parse_nvidia_smi_csv("garbage") == []
    assert (
        parse_cuda_driver_version(
            "| NVIDIA-SMI 550.120   Driver Version: 550.120   CUDA Version: 12.4 |"
        )
        == "12.4"
    )  # noqa: E501
    assert parse_cuda_driver_version("KMD Version: 610.88   CUDA UMD Version: 13.3") == "13.3"
    assert parse_meminfo("MemTotal:       32768000 kB\nMemFree: 1 kB\n") == 32000
    assert architecture(7.5) == "turing" and architecture(None) == "unknown"


def test_profile_ids_come_from_hardware_not_hostnames():
    assert suggest_profile(inventory(GTX1070)) == "linux_pascal_8gb"
    assert suggest_profile(inventory(RTX5080, os="windows")) == "windows_blackwell_16gb"
    assert suggest_profile(inventory(os="darwin", ram_mb=8192)) == "macos_cpu_8gb"


def test_pascal_needs_the_cuda12_build():
    pascal = cuda_compatibility(inventory(GTX1070))
    assert pascal["required_crispasr_build"] == "cuda12" and pascal["cuda13_supported"] is False
    assert cuda_compatibility(inventory(RTX5080))["required_crispasr_build"] == "cuda13 or cuda12"
    assert cuda_compatibility(inventory())["cuda"] is False


def test_memory_strategy_is_explicit_and_recorded():
    config = CrispAsrConfig(env={"LD_LIBRARY_PATH": "/x", "CRISPASR_KV_QUANT": "q8_0"})
    assert effective_memory(config, None) == {
        "strategy": "default",
        "env": {"CRISPASR_KV_QUANT": "q8_0"},  # only memory-relevant variables
        "extra_args": [],
    }
    offload = ModelRuntime(strategy="kv-cpu", env={"CRISPASR_KV_ON_CPU": "1"})
    assert effective_memory(config, offload)["env"] == {
        "CRISPASR_KV_ON_CPU": "1",
        "CRISPASR_KV_QUANT": "q8_0",
    }


def test_log_classification():
    assert classify_log("ggml_cuda_init: found 1 CUDA devices")["cuda"]
    assert classify_log("CUDA error: out of memory")["oom"]
    assert classify_log("CUDA error: no kernel image is available for execution on the device")[
        "arch_unsupported"
    ]


def measured(**kw) -> Measurement:
    base = dict(loaded=True, requests=10, errors=0, non_empty=9, audio_ms=30_000,
                inference_ms=3_000, device="cuda")  # fmt: skip
    m = Measurement()
    for k, v in (base | kw).items():
        setattr(m, k, v)
    return m


@pytest.mark.parametrize(
    ("kwargs", "strategy", "state"),
    [
        ({}, "default", PlatformState.QUALIFIED),
        ({"device": "cpu"}, "default", PlatformState.QUALIFIED_CPU_ONLY),
        ({}, "kv-cpu", PlatformState.QUALIFIED_WITH_OFFLOAD),
        ({"inference_ms": 45_000}, "default", PlatformState.TOO_SLOW),
        ({"loaded": False, "log_flags": {"oom": True}}, "default", PlatformState.OOM),
        (
            {"loaded": False, "log_flags": {"arch_unsupported": True}},
            "default",
            PlatformState.UNSUPPORTED_ON_PLATFORM,
        ),  # fmt: skip
        ({"loaded": False}, "default", PlatformState.BACKEND_FAILURE),
        ({"crashed": True, "log_flags": {"oom": True}}, "default", PlatformState.OOM),
        ({"errors": 5}, "default", PlatformState.BACKEND_FAILURE),
        ({"non_empty": 1}, "default", PlatformState.BACKEND_FAILURE),  # loads, useless output
        ({"requests": 0, "non_empty": 0}, "default", PlatformState.BACKEND_FAILURE),
    ],
)
def test_classification(kwargs, strategy, state):
    assert classify(measured(**kwargs), strategy, Thresholds())[0] == state
