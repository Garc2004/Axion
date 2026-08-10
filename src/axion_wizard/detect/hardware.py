"""Detección de hardware: RAM, CPU y GPU (§4.1)."""

from __future__ import annotations

import platform as _platform
from dataclasses import dataclass, field

import psutil

from axion_wizard.utils.shell import CommandNotFoundError, CommandTimeoutError, run


@dataclass
class GpuInfo:
    vendor: str  # "nvidia" | "amd" | "intel" | "unknown"
    name: str | None = None
    vram_mb: int | None = None


@dataclass
class HardwareInfo:
    ram_total_bytes: int
    cpu_logical: int
    cpu_physical: int | None
    gpus: list[GpuInfo] = field(default_factory=list)

    @property
    def ram_total_gb(self) -> float:
        return self.ram_total_bytes / (1024**3)

    @property
    def has_gpu(self) -> bool:
        return len(self.gpus) > 0


def detect_ram_bytes() -> int:
    return psutil.virtual_memory().total


def detect_cpu_counts() -> tuple[int, int | None]:
    return psutil.cpu_count(logical=True) or 1, psutil.cpu_count(logical=False)


def parse_nvidia_smi_csv(output: str) -> list[GpuInfo]:
    gpus = []
    for line in output.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 2:
            continue
        name, mem = parts
        vram_mb = _safe_int(mem)
        gpus.append(GpuInfo(vendor="nvidia", name=name or None, vram_mb=vram_mb))
    return gpus


def _safe_int(value: str) -> int | None:
    try:
        return int(float(value))
    except ValueError:
        return None


def detect_nvidia_gpus(timeout: float = 5.0) -> list[GpuInfo]:
    try:
        result = run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            timeout=timeout,
        )
    except (CommandNotFoundError, CommandTimeoutError):
        return []
    if not result.ok:
        return []
    return parse_nvidia_smi_csv(result.stdout)


def parse_rocm_smi_output(output: str) -> list[GpuInfo]:
    gpus = []
    for line in output.strip().splitlines():
        if "GPU" not in line or ":" not in line:
            continue
        name = line.split(":", 1)[1].strip()
        if name:
            gpus.append(GpuInfo(vendor="amd", name=name))
    return gpus


def detect_rocm_gpus(timeout: float = 5.0) -> list[GpuInfo]:
    try:
        result = run(["rocm-smi", "--showproductname"], timeout=timeout)
    except (CommandNotFoundError, CommandTimeoutError):
        return []
    if not result.ok:
        return []
    return parse_rocm_smi_output(result.stdout)


def classify_gpu_vendor(name: str) -> str:
    lowered = name.lower()
    if "nvidia" in lowered:
        return "nvidia"
    if "amd" in lowered or "radeon" in lowered:
        return "amd"
    if "intel" in lowered:
        return "intel"
    return "unknown"


def parse_wmi_video_controllers(output: str) -> list[GpuInfo]:
    gpus = []
    for line in output.strip().splitlines():
        name = line.strip()
        if not name:
            continue
        gpus.append(GpuInfo(vendor=classify_gpu_vendor(name), name=name))
    return gpus


def detect_windows_gpus(timeout: float = 10.0) -> list[GpuInfo]:
    """Fallback vía WMI cuando no hay `nvidia-smi`/`rocm-smi` en PATH."""
    if _platform.system() != "Windows":
        return []
    try:
        result = run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name",
            ],
            timeout=timeout,
        )
    except (CommandNotFoundError, CommandTimeoutError):
        return []
    if not result.ok:
        return []
    return parse_wmi_video_controllers(result.stdout)


def detect_gpus() -> list[GpuInfo]:
    gpus = detect_nvidia_gpus()
    gpus += detect_rocm_gpus()
    if not gpus:
        gpus = detect_windows_gpus()
    return gpus


def detect_hardware() -> HardwareInfo:
    ram = detect_ram_bytes()
    logical, physical = detect_cpu_counts()
    gpus = detect_gpus()
    return HardwareInfo(ram_total_bytes=ram, cpu_logical=logical, cpu_physical=physical, gpus=gpus)
