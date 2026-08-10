"""Datos que los pasos del flujo de instalación se pasan entre sí (§4).

Vive aparte de `base.py` para que los pasos puedan importar el contexto sin
arrastrar la clase `Step`, y para no crear un ciclo con `cli.GlobalState`.

Nada de esto se persiste: `.axion-wizard-state.json` guarda solo qué pasos
terminaron (§4, y ver `utils.state`), nunca los valores — aquí dentro viaja
la contraseña de PostgreSQL en claro. Al reanudar, cada paso repuebla lo
suyo con `Step.restore()` leyendo los artefactos que ya escribió en disco.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

# En tiempo de ejecución, no solo para tipos: `gpu_acceleration` necesita su
# valor por defecto. `detect.docker` solo depende de `utils`, así que no cierra
# ningún ciclo de importación.
from axion_wizard.detect import docker as detect_docker

if TYPE_CHECKING:
    from axion_wizard.config import AxionConfig
    from axion_wizard.detect.docker import DockerInfo
    from axion_wizard.detect.hardware import HardwareInfo
    from axion_wizard.detect.platform import OsInfo, WslInfo


@dataclass
class EnvironmentFacts:
    """Lo que produce el paso 1 y de lo que depende todo lo demás (§4.1)."""

    os_info: OsInfo
    wsl: WslInfo
    docker: DockerInfo
    hardware: HardwareInfo
    #: `host` (Linux nativo) o `ports` (Docker Desktop/Windows). La salida
    #: decisiva de §4.1: el resto del flujo se ramifica sobre esto.
    wireguard_variant: str
    #: Cómo se le entrega la GPU a Ollama: `none`, `nvidia` o `rocm`. Distinto
    #: de "hay una GPU": el valor solo deja de ser `none` si Docker de verdad
    #: pudo pasarla a un contenedor de prueba, porque reservarla sin
    #: comprobarlo deja el contenedor parado en `created` para siempre en GPUs
    #: sin soporte de passthrough bajo WSL2 (§7, incidente real). Cada modo
    #: implica además una imagen de Ollama distinta.
    gpu_acceleration: str = detect_docker.GPU_ACCELERATION_NONE

    @property
    def gpu_passthrough_works(self) -> bool:
        return self.gpu_acceleration != detect_docker.GPU_ACCELERATION_NONE


@dataclass
class NetworkFacts:
    """Lo que produce el paso 2 (§4.2)."""

    lan_ip: str | None = None
    interface_name: str | None = None
    public_ip: str | None = None
    #: `True` si el IP público no coincide con el WAN del router: el port
    #: forwarding no llegará nunca y solo queda el acceso por LAN.
    cgnat: bool = False
    busy_ports: list[str] = field(default_factory=list)
    unreachable_targets: list[str] = field(default_factory=list)


@dataclass
class InstallContext:
    """Estado en memoria de una ejecución de `install`."""

    project_dir: Path
    environment: EnvironmentFacts | None = None
    network: NetworkFacts | None = None
    config: AxionConfig | None = None
    #: Rellenado por el paso 4; el paso 9 lo vuelve a leer de disco.
    cert_path: Path | None = None
    #: Advertencias no fatales acumuladas, para el resumen final.
    warnings: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def require_environment(self) -> EnvironmentFacts:
        """Acceso comprobado, para que un paso mal ordenado falle aquí y no
        con un `AttributeError` sobre `None` tres llamadas más abajo."""
        if self.environment is None:
            raise RuntimeError("el paso 1 (entorno) no se ha ejecutado todavía")
        return self.environment

    def require_config(self) -> AxionConfig:
        if self.config is None:
            raise RuntimeError("el paso 3 (configuración) no se ha ejecutado todavía")
        return self.config
