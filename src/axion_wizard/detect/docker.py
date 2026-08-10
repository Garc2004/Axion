"""Docker engine detection: version, Compose v2 and active context (§4.1)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from axion_wizard.utils.jsonio import parse_json_lines_or_array
from axion_wizard.utils.shell import CommandNotFoundError, CommandTimeoutError, run

DESKTOP_CONTEXT_NAME = "desktop-linux"

#: How the GPU is handed to Ollama. Each value implies a different image and
#: a different compose section; `none` means CPU inference.
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
    """`docker context ls --format json` can emit a whole JSON array or one
    JSON object per line depending on the CLI version; both are supported."""
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
    """An actual test of whether Docker can hand a GPU to a container.

    Detecting the GPU with `nvidia-smi` (`detect.hardware.detect_gpus`) is not
    enough: passthrough under Docker Desktop/WSL2 requires a compatible
    driver, WSL2 version and *compute capability*, and with an old GPU
    (Kepler architecture or earlier, say) the `nvidia-container-cli` hook
    fails to start ANY container with `--gpus`, even though `nvidia-smi` sees
    it perfectly well:

        nvidia-container-cli: initialization error: WSL environment
        detected but no adapters were found

    Without this test, the compose file reserves the GPU for `ollama`
    unconditionally whenever a GPU is present, and that container sits in
    `created` forever — cascading into `fastapi` (which depends on it) and,
    if `mattermost` was waiting on something too, into `nginx`. The probe uses
    a minimal image (`busybox`), pulled if needed; the default timeout is
    generous to cover that pull on the first run.
    """
    try:
        result = run(["docker", "run", "--rm", "--gpus", "all", "busybox", "true"], timeout=timeout)
    except (CommandNotFoundError, CommandTimeoutError):
        return False
    return result.ok


def docker_rocm_passthrough_works(timeout: float = 60.0) -> bool:
    """An actual test of whether Docker can hand an AMD GPU to a container.

    ROCm does not go through NVIDIA's runtime: `--gpus` does nothing for it.
    The GPU is handed over as two kernel devices, `/dev/kfd` (the compute
    driver) and `/dev/dri` (the render node), so the probe has to be that one
    and not `docker_gpu_passthrough_works` — using the latter always came back
    negative on perfectly capable AMD machines, and the GPU went unused with
    nothing to explain why.

    It fails, correctly, when the kernel does not carry `amdgpu` or the user
    is not in the `video`/`render` groups.
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
