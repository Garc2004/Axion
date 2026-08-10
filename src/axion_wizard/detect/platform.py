"""Detección de SO, WSL y systemd (§4.1 de la spec).

La salida decisiva de este módulo, combinada con `detect.docker`, es
`decide_wireguard_variant()`: determina si el resto del flujo usa la
variante `host` (Linux nativo) o `ports` (Docker Desktop / Windows).
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
    """Lee `/proc/version` buscando "microsoft" (case-insensitive) y lo
    confirma con la variable de entorno `WSL_DISTRO_NAME`."""
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
    """`wsl.exe` históricamente emite UTF-16; si se decodifica como si fuera
    UTF-8 aparecen bytes nulos intercalados. Los limpiamos a la fuerza."""
    return text.replace("\x00", "")


def parse_wsl_list_verbose(output: str, distro_name: str | None = None) -> int | None:
    """Parsea la salida de `wsl.exe -l -v` y devuelve la versión (1 o 2) de
    `distro_name`, o de la distro por defecto (marcada con `*`) si no se
    especifica ninguna."""
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
    """`wsl.exe -l -v` es la fuente primaria (funciona también invocado desde
    dentro de una distro vía interop); si no está disponible, la presencia de
    `/run/WSL` es un indicador fiable de WSL2."""
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
    """Encuentra `.wslconfig` del usuario de Windows vía `/mnt/c/Users/...`.

    Solo sirve corriendo *dentro* de WSL — `/mnt/c` no existe en Windows
    nativo. Para ese caso está `locate_wslconfig_native`.
    """
    if not mnt_c_users.exists():
        return None
    for candidate in sorted(mnt_c_users.glob("*/.wslconfig")):
        if candidate.is_file():
            return candidate
    return None


def locate_wslconfig_native(home: Path | None = None) -> Path | None:
    """Encuentra `.wslconfig` corriendo como binario nativo de Windows.

    `axion-wizard.exe` normalmente no corre dentro de WSL — corre en
    Windows y es Docker Desktop quien usa WSL2 por debajo. Ahí `/mnt/c` no
    existe, pero el archivo es el mismo de siempre: `%USERPROFILE%\\.wslconfig`,
    accesible directamente vía `Path.home()`.
    """
    candidate = (home if home is not None else Path.home()) / ".wslconfig"
    return candidate if candidate.is_file() else None


def is_mirrored_networking_configured(wslconfig_path: Path | None) -> bool:
    """Parsea `.wslconfig` buscando `networkingMode=mirrored` bajo `[wsl2]`."""
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
    """Si mirrored está mal aplicado, `eth0` sigue dando un IP interno de
    Docker Desktop (`172.16.0.0/12`) en vez del IP de la LAN real."""
    if not ip_str:
        return False
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return ip in MIRRORED_FORBIDDEN_NETWORK


def mirrored_networking_is_active(wslconfig_path: Path | None, eth0_ip: str | None) -> bool:
    """Mirrored está realmente activo solo si está configurado en
    `.wslconfig` Y `eth0` no cayó de vuelta al rango interno."""
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
    """`host` solo aplica en Linux nativo con Docker Engine (no Desktop).
    Windows y cualquier contexto `desktop-linux` usan `ports`."""
    if os_name == "Linux" and not docker_context_is_desktop:
        return WIREGUARD_VARIANT_HOST
    return WIREGUARD_VARIANT_PORTS
