"""Detección del motor Docker: versión, Compose v2 y contexto activo (§4.1)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from axion_wizard.utils.jsonio import parse_json_lines_or_array
from axion_wizard.utils.shell import CommandNotFoundError, CommandTimeoutError, run

DESKTOP_CONTEXT_NAME = "desktop-linux"

#: Cómo se le entrega la GPU a Ollama. Cada valor implica una imagen y una
#: sección de compose distintas; `none` significa inferencia en CPU.
GPU_ACCELERATION_NONE = "none"
GPU_ACCELERATION_NVIDIA = "nvidia"
GPU_ACCELERATION_ROCM = "rocm"


@dataclass
class DockerContextInfo:
    active_context: str | None
    is_desktop: bool
    contexts: list[str] = field(default_factory=list)


@dataclass
class DockerInfo:
    installed: bool
    docker_version: str | None
    compose_version: str | None
    compose_is_v2: bool
    context: DockerContextInfo


def get_docker_version(timeout: float = 10.0) -> str | None:
    try:
        result = run(["docker", "--version"], timeout=timeout)
    except (CommandNotFoundError, CommandTimeoutError):
        return None
    if not result.ok:
        return None
    return result.stdout.strip() or None


_COMPOSE_VERSION_RE = re.compile(r"v?(\d+)\.")


def is_compose_v2(version_str: str) -> bool:
    match = _COMPOSE_VERSION_RE.match(version_str.strip())
    if not match:
        return False
    return int(match.group(1)) >= 2


def get_compose_version(timeout: float = 10.0) -> tuple[str | None, bool]:
    try:
        result = run(["docker", "compose", "version", "--short"], timeout=timeout)
    except (CommandNotFoundError, CommandTimeoutError):
        return None, False
    if not result.ok:
        return None, False
    version_str = result.stdout.strip()
    if not version_str:
        return None, False
    return version_str, is_compose_v2(version_str)


def parse_context_ls(output: str) -> list[dict]:
    """`docker context ls --format json` puede emitir un array JSON completo
    o un objeto JSON por línea según la versión de la CLI; soportamos ambos."""
    return parse_json_lines_or_array(output)


def get_docker_contexts(timeout: float = 10.0) -> DockerContextInfo:
    try:
        result = run(["docker", "context", "ls", "--format", "json"], timeout=timeout)
    except (CommandNotFoundError, CommandTimeoutError):
        return DockerContextInfo(active_context=None, is_desktop=False, contexts=[])
    if not result.ok:
        return DockerContextInfo(active_context=None, is_desktop=False, contexts=[])

    entries = parse_context_ls(result.stdout)
    names = []
    active = None
    for entry in entries:
        name = entry.get("Name")
        if name:
            names.append(name)
        if entry.get("Current") is True:
            active = name

    return DockerContextInfo(
        active_context=active,
        is_desktop=active == DESKTOP_CONTEXT_NAME,
        contexts=names,
    )


def gather_docker_info(timeout: float = 10.0) -> DockerInfo:
    docker_version = get_docker_version(timeout=timeout)
    compose_version, compose_is_v2 = get_compose_version(timeout=timeout)
    context = get_docker_contexts(timeout=timeout)
    return DockerInfo(
        installed=docker_version is not None,
        docker_version=docker_version,
        compose_version=compose_version,
        compose_is_v2=compose_is_v2,
        context=context,
    )


def docker_gpu_passthrough_works(timeout: float = 60.0) -> bool:
    """Prueba real de si Docker puede pasarle una GPU a un contenedor.

    Detectar la GPU con `nvidia-smi` (`detect.hardware.detect_gpus`) no
    basta: el passthrough en Docker Desktop/WSL2 exige controlador, versión
    de WSL2 y *compute capability* compatibles, y con una GPU vieja
    (arquitectura Kepler o anterior, p.ej.) el hook de
    `nvidia-container-cli` falla al arrancar CUALQUIER contenedor con
    `--gpus`, aunque `nvidia-smi` la vea perfectamente:

        nvidia-container-cli: initialization error: WSL environment
        detected but no adapters were found

    Sin esta prueba, el compose reserva la GPU para `ollama` incondicional-
    mente cuando hay una GPU presente, y ese contenedor se queda parado en
    `created` para siempre — arrastrando en cascada a `fastapi` (depende de
    él) y, si `mattermost` también estaba esperando algo, a `nginx`. Se
    prueba con una imagen mínima (`busybox`), que se descarga si hace
    falta; el timeout por defecto es generoso para cubrir esa descarga en
    la primera ejecución.
    """
    try:
        result = run(["docker", "run", "--rm", "--gpus", "all", "busybox", "true"], timeout=timeout)
    except (CommandNotFoundError, CommandTimeoutError):
        return False
    return result.ok


def docker_rocm_passthrough_works(timeout: float = 60.0) -> bool:
    """Prueba real de si Docker puede pasarle una GPU AMD a un contenedor.

    ROCm no pasa por el runtime de NVIDIA: `--gpus` no le sirve de nada. La
    GPU se entrega como dos dispositivos del kernel, `/dev/kfd` (el driver de
    cómputo) y `/dev/dri` (el nodo de render), así que la prueba tiene que ser
    esa y no la de `docker_gpu_passthrough_works` — usar aquella daba siempre
    negativo en equipos AMD perfectamente capaces, y la GPU se quedaba sin
    usar sin que nada lo explicara.

    Falla, correctamente, cuando el kernel no trae `amdgpu` o el usuario no
    está en los grupos `video`/`render`.
    """
    try:
        result = run(
            [
                "docker",
                "run",
                "--rm",
                "--device",
                "/dev/kfd",
                "--device",
                "/dev/dri",
                "busybox",
                "true",
            ],
            timeout=timeout,
        )
    except (CommandNotFoundError, CommandTimeoutError):
        return False
    return result.ok
