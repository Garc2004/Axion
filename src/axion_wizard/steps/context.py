"""Data the install steps hand to each other (§4).

It lives apart from `base.py` so steps can import the context without
dragging in the `Step` class, and to avoid a cycle with `cli.GlobalState`.

None of this is persisted: `.axion-wizard-state.json` records only which
steps finished (§4, and see `utils.state`), never the values — the PostgreSQL
password travels through here in the clear. On resume, each step repopulates
its own part with `Step.restore()` by reading the artifacts it already wrote
to disk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

# At runtime, not just for typing: `gpu_acceleration` needs its default
# value. `detect.docker` depends only on `utils`, so this closes no import
# cycle.
from axion_wizard.detect import docker as detect_docker

if TYPE_CHECKING:
    from axion_wizard.detect.docker import DockerInfo
    from axion_wizard.detect.hardware import HardwareInfo
    from axion_wizard.detect.platform import OsInfo, WslInfo
    from axion_wizard.domain.config import AxionConfig


@dataclass
class EnvironmentFacts:
    """What step 1 produces and everything else depends on (§4.1)."""

    os_info: OsInfo
    wsl: WslInfo
    docker: DockerInfo
    hardware: HardwareInfo
    #: `host` (native Linux) or `ports` (Docker Desktop/Windows). §4.1's
    #: decisive output: the rest of the flow branches on it.
    wireguard_variant: str
    #: How the GPU is handed to Ollama: `none`, `nvidia` or `rocm`. Distinct
    #: from "there is a GPU": the value only stops being `none` if Docker
    #: genuinely managed to pass one to a test container, because reserving it
    #: without checking leaves the container stuck in `created` forever on
    #: GPUs with no passthrough support under WSL2 (§7, a real incident). Each
    #: mode also implies a different Ollama image.
    gpu_acceleration: str = detect_docker.GPU_ACCELERATION_NONE
    #: Whether the container runtime's kernel can run IPv6 netfilter rules —
    #: only meaningful for the `ports` variant, which is the only one the
    #: compose template consults it for (native Linux's `host` variant has
    #: `network_mode: host` and no IPv6 handling to switch off). Defaults to
    #: `False` — the same "assume the safe/off setting until proven otherwise"
    #: choice as `gpu_acceleration`'s default: wrongly assuming it works
    #: reproduces the incident this exists to prevent (wg-easy's `PostUp`
    #: aborting and leaving the tunnel dead); wrongly assuming it is broken
    #: only costs IPv6 inside a VPN tunnel nobody is relying on for that.
    wireguard_ipv6_supported: bool = False

    @property
    def gpu_passthrough_works(self) -> bool:
        return self.gpu_acceleration != detect_docker.GPU_ACCELERATION_NONE


@dataclass
class NetworkFacts:
    """What step 2 produces (§4.2)."""

    lan_ip: str | None = None
    interface_name: str | None = None
    public_ip: str | None = None
    #: `True` if the public IP does not match the router's WAN address: port
    #: forwarding will never arrive and only LAN access remains.
    cgnat: bool = False
    busy_ports: list[str] = field(default_factory=list)
    unreachable_targets: list[str] = field(default_factory=list)


@dataclass
class InstallContext:
    """In-memory state of one `install` run."""

    project_dir: Path
    environment: EnvironmentFacts | None = None
    network: NetworkFacts | None = None
    config: AxionConfig | None = None
    #: Filled in by step 4; step 9 reads it back off disk.
    cert_path: Path | None = None
    #: Accumulated non-fatal warnings, for the closing summary.
    warnings: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def require_environment(self) -> EnvironmentFacts:
        """Checked access, so a misordered step fails here rather than with an
        `AttributeError` on `None` three calls further down."""
        if self.environment is None:
            raise RuntimeError("step 1 (environment) has not run yet")
        return self.environment

    def require_config(self) -> AxionConfig:
        if self.config is None:
            raise RuntimeError("step 3 (configuration) has not run yet")
        return self.config
