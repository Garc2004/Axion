"""OS, WSL and systemd detection (§4.1 of the spec).

This module's decisive output, combined with `detect.docker`, is
`decide_wireguard_variant()`: it determines whether the rest of the flow uses
the `host` variant (native Linux) or `ports` (Docker Desktop / Windows).
"""

from __future__ import annotations

import ipaddress
import os
import platform as _platform
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from axion_wizard.utils.shell import CommandNotFoundError, CommandTimeoutError, run

WIREGUARD_VARIANT_HOST = "host"
WIREGUARD_VARIANT_PORTS = "ports"

MIRRORED_FORBIDDEN_NETWORK = ipaddress.ip_network("172.16.0.0/12")

DEFAULT_PROC_VERSION = Path("/proc/version")
DEFAULT_RUN_WSL_MARKER = Path("/run/WSL")
DEFAULT_MNT_C_USERS = Path("/mnt/c/Users")


@dataclass
class OsInfo:
    name: str
    release: str


@dataclass
class WslInfo:
    inside_wsl: bool
    distro_name: str | None = None
    version: int | None = None
    wslconfig_path: Path | None = None
    mirrored_configured: bool = False


def get_os_info() -> OsInfo:
    return OsInfo(name=_platform.system(), release=_platform.release())


def is_inside_wsl(
    proc_version_path: Path = DEFAULT_PROC_VERSION,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Read `/proc/version` looking for "microsoft" (case-insensitive) and
    corroborate it with the `WSL_DISTRO_NAME` environment variable."""
    env = env if env is not None else os.environ
    has_env_marker = bool(env.get("WSL_DISTRO_NAME"))
    try:
        text = proc_version_path.read_text(errors="ignore")
    except OSError:
        return has_env_marker
    has_kernel_marker = "microsoft" in text.lower()
    return has_kernel_marker or has_env_marker


def get_wsl_distro_name(env: Mapping[str, str] | None = None) -> str | None:
    env = env if env is not None else os.environ
    return env.get("WSL_DISTRO_NAME")


def _clean_wsl_exe_output(text: str) -> str:
    """`wsl.exe` has historically emitted UTF-16; decoding that as UTF-8
    leaves interleaved null bytes. Strip them by force."""
    return text.replace("\x00", "")


def parse_wsl_list_verbose(output: str, distro_name: str | None = None) -> int | None:
    """Parse the output of `wsl.exe -l -v` and return the version (1 or 2) of
    `distro_name`, or of the default distro (marked with `*`) if none is
    given."""
    text = _clean_wsl_exe_output(output)
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.upper().startswith("NAME"):
            continue
        is_default = line.startswith("*")
        line = line.lstrip("*").strip()
        parts = line.split()
        if len(parts) < 3:
            continue
        name, _state, version = parts[0], parts[1], parts[2]
        if distro_name:
            if name.lower() == distro_name.lower():
                return _safe_int(version)
        elif is_default:
            return _safe_int(version)
    return None


def _safe_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def detect_wsl_version(
    distro_name: str | None = None,
    run_wsl_marker: Path = DEFAULT_RUN_WSL_MARKER,
    timeout: float = 10.0,
) -> int | None:
    """`wsl.exe -l -v` is the primary source (it works even when invoked from
    inside a distro via interop); if it is unavailable, the presence of
    `/run/WSL` is a reliable indicator of WSL2."""
    try:
        result = run(["wsl.exe", "-l", "-v"], timeout=timeout)
    except (CommandNotFoundError, CommandTimeoutError):
        result = None

    if result is not None and result.ok:
        version = parse_wsl_list_verbose(result.stdout, distro_name)
        if version is not None:
            return version

    return 2 if run_wsl_marker.exists() else None


def locate_wslconfig(mnt_c_users: Path = DEFAULT_MNT_C_USERS) -> Path | None:
    """Find the Windows user's `.wslconfig` via `/mnt/c/Users/...`.

    Only works when running *inside* WSL — `/mnt/c` does not exist on native
    Windows. `locate_wslconfig_native` covers that case.
    """
    if not mnt_c_users.exists():
        return None
    for candidate in sorted(mnt_c_users.glob("*/.wslconfig")):
        if candidate.is_file():
            return candidate
    return None


def locate_wslconfig_native(home: Path | None = None) -> Path | None:
    """Find `.wslconfig` when running as a native Windows binary.

    `axion-wizard.exe` usually does not run inside WSL — it runs on Windows
    and it is Docker Desktop that uses WSL2 underneath. There `/mnt/c` does
    not exist, but the file is the same one as always:
    `%USERPROFILE%\\.wslconfig`, reachable directly via `Path.home()`.
    """
    candidate = (home if home is not None else Path.home()) / ".wslconfig"
    return candidate if candidate.is_file() else None


def is_mirrored_networking_configured(wslconfig_path: Path | None) -> bool:
    """Parse `.wslconfig` looking for `networkingMode=mirrored` under `[wsl2]`."""
    if wslconfig_path is None or not wslconfig_path.exists():
        return False
    try:
        text = wslconfig_path.read_text(errors="ignore")
    except OSError:
        return False

    section: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if section == "wsl2" and "=" in line:
            key, _, value = line.partition("=")
            if key.strip().lower() == "networkingmode" and value.strip().lower() == "mirrored":
                return True
    return False


def is_eth0_in_forbidden_range(ip_str: str | None) -> bool:
    """When mirrored is not really in effect, `eth0` still reports an
    internal Docker Desktop address (`172.16.0.0/12`) instead of the real LAN
    IP."""
    if not ip_str:
        return False
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return ip in MIRRORED_FORBIDDEN_NETWORK


def mirrored_networking_is_active(wslconfig_path: Path | None, eth0_ip: str | None) -> bool:
    """Mirrored is genuinely active only if it is configured in `.wslconfig`
    AND `eth0` did not fall back to the internal range."""
    configured = is_mirrored_networking_configured(wslconfig_path)
    return configured and not is_eth0_in_forbidden_range(eth0_ip)


def is_systemd_active(timeout: float = 5.0) -> bool:
    try:
        result = run(["ps", "-p", "1", "-o", "comm="], timeout=timeout)
    except (CommandNotFoundError, CommandTimeoutError):
        return False
    return result.ok and "systemd" in result.stdout.strip().lower()


def gather_wsl_info(
    proc_version_path: Path = DEFAULT_PROC_VERSION,
    mnt_c_users: Path = DEFAULT_MNT_C_USERS,
    env: Mapping[str, str] | None = None,
) -> WslInfo:
    inside = is_inside_wsl(proc_version_path=proc_version_path, env=env)
    if not inside:
        return WslInfo(inside_wsl=False)

    distro = get_wsl_distro_name(env=env)
    version = detect_wsl_version(distro_name=distro)
    wslconfig_path = locate_wslconfig(mnt_c_users=mnt_c_users)
    mirrored_configured = is_mirrored_networking_configured(wslconfig_path)
    return WslInfo(
        inside_wsl=True,
        distro_name=distro,
        version=version,
        wslconfig_path=wslconfig_path,
        mirrored_configured=mirrored_configured,
    )


def decide_wireguard_variant(os_name: str, docker_context_is_desktop: bool) -> str:
    """`host` applies only on native Linux with Docker Engine (not Desktop).
    Windows and any `desktop-linux` context use `ports`."""
    if os_name == "Linux" and not docker_context_is_desktop:
        return WIREGUARD_VARIANT_HOST
    return WIREGUARD_VARIANT_PORTS
