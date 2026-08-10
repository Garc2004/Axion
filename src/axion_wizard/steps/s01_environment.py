"""Paso 1 — Detección de entorno (§4.1).

Produce `wireguard_variant`, la salida decisiva de la que depende todo el
resto del flujo: `host` en Linux nativo con Docker Engine, `ports` en
Windows o bajo cualquier contexto de Docker Desktop.

Este paso no escribe nada; solo mira y decide. Por eso falla temprano y con
un mensaje accionable ante lo que haría inviable el despliegue —Docker
ausente, Compose v1— en vez de dejar que reviente ocho pasos más adelante.
"""

from __future__ import annotations

from rich.table import Table

from axion_wizard.detect import docker as detect_docker
from axion_wizard.detect import platform as detect_platform
from axion_wizard.detect.hardware import HardwareInfo, detect_hardware
from axion_wizard.domain.config import WireguardVariant
from axion_wizard.errors import PlatformError
from axion_wizard.render import ui
from axion_wizard.render.console import console
from axion_wizard.steps.base import Step, StepResult
from axion_wizard.steps.context import EnvironmentFacts

#: Prefijo del filesystem de Windows montado dentro de WSL. Un `project_dir`
#: aquí funciona, pero con I/O cruzado lento y sin permisos POSIX (§6.2).
WINDOWS_MOUNT_PREFIX = "/mnt/"


class EnvironmentStep(Step):
    name = "environment"
    title = "Detección de entorno"

    def run(self) -> StepResult:
        os_info = detect_platform.get_os_info()
        wsl = detect_platform.gather_wsl_info()
        docker = detect_docker.gather_docker_info()
        hardware = detect_hardware()

        self._assert_docker_is_usable(docker)

        variant = detect_platform.decide_wireguard_variant(
            os_info.name, docker.context.is_desktop
        )
        gpu_acceleration = self._check_gpu_passthrough(hardware)
        facts = EnvironmentFacts(
            os_info=os_info,
            wsl=wsl,
            docker=docker,
            hardware=hardware,
            wireguard_variant=variant,
            gpu_acceleration=gpu_acceleration,
        )
        self.context.environment = facts

        self._warn_about_crossed_filesystem(wsl)
        self._warn_about_broken_mirrored(wsl)
        self._warn_about_windows_docker_desktop_lan_exposure(os_info, wsl, docker, variant)

        if not self.state.quiet:
            console.print(self._render_table(facts))

        return StepResult(
            name=self.name,
            ok=True,
            data={"wireguard_variant": variant},
            message=f"{os_info.name} {os_info.release}, variante WireGuard `{variant}`",
        )

    def verify(self) -> StepResult:
        """Re-detecta y confirma que el entorno sigue sirviendo.

        No es paranoia: entre una ejecución y la siguiente el usuario puede
        haber cambiado el contexto de Docker (`docker context use`) —y el
        compose ya renderizado dejaría de corresponder a la plataforma— o
        haber desinstalado Docker por completo, en cuyo caso *nada* de lo
        que viene después puede funcionar y seguir adelante solo retrasa el
        error hasta un paso que no tiene la culpa.
        """
        docker = detect_docker.gather_docker_info()
        os_info = detect_platform.get_os_info()

        if not docker.installed:
            return StepResult(
                name=self.name, ok=False, message="Docker ya no está disponible en este sistema"
            )
        if not docker.compose_is_v2:
            return StepResult(
                name=self.name, ok=False, message="Docker Compose v2 ya no está disponible"
            )

        variant = detect_platform.decide_wireguard_variant(
            os_info.name, docker.context.is_desktop
        )
        expected = self.context.require_environment().wireguard_variant
        if variant != expected:
            return StepResult(
                name=self.name,
                ok=False,
                message=f"la variante cambió de `{expected}` a `{variant}`",
            )
        return StepResult(name=self.name, ok=True, message=f"variante `{variant}`")

    def restore(self) -> None:
        """Al reanudar hay que volver a detectar: nada de esto se persiste, y
        re-detectar es barato y no toca el sistema."""
        self.run()

    # --- comprobaciones de viabilidad -------------------------------------------

    def _assert_docker_is_usable(self, docker: detect_docker.DockerInfo) -> None:
        if not docker.installed:
            raise PlatformError(
                what="No se encontró Docker en este sistema",
                why=(
                    "Todo el stack AXION corre en contenedores; sin el motor de "
                    "Docker no hay nada que desplegar."
                ),
                steps=[
                    "Windows: instalar Docker Desktop y arrancarlo "
                    "(https://docs.docker.com/desktop/install/windows-install/).",
                    "Linux: instalar Docker Engine "
                    "(https://docs.docker.com/engine/install/).",
                    "Comprobar que responde: docker --version",
                ],
            )
        if not docker.compose_is_v2:
            detected = docker.compose_version or "no detectada"
            raise PlatformError(
                what=f"Se necesita Docker Compose v2 y se encontró: {detected}",
                why=(
                    "El compose que genera el wizard usa sintaxis de Compose v2 "
                    "(`docker compose`, sin guion). Con v1 (`docker-compose`) el "
                    "despliegue falla con errores de esquema difíciles de leer."
                ),
                steps=[
                    "Actualizar Docker Desktop a una versión reciente (trae Compose v2).",
                    "Linux: instalar el plugin `docker-compose-plugin` de la distro.",
                    "Comprobar: docker compose version",
                ],
            )

    def _check_gpu_passthrough(self, hardware: HardwareInfo) -> str:
        """Decide cómo se le entrega la GPU a Ollama, probándolo de verdad.

        Cada fabricante se entrega por un mecanismo distinto y hay que probar
        el que corresponde: NVIDIA por el runtime (`--gpus`), AMD por los
        dispositivos del kernel (`/dev/kfd`, `/dev/dri`). Probar el de NVIDIA
        en un equipo AMD da negativo siempre, y la GPU se quedaba sin usar sin
        que nada lo explicara.

        Sin GPU detectada no hay nada que probar — y probar igual costaría una
        descarga de imagen innecesaria en la mayoría de instalaciones.
        """
        if not hardware.has_gpu:
            return detect_docker.GPU_ACCELERATION_NONE

        vendors = {gpu.vendor for gpu in hardware.gpus}
        gpu_label = ", ".join(g.name or g.vendor for g in hardware.gpus)

        if "nvidia" in vendors and detect_docker.docker_gpu_passthrough_works():
            return detect_docker.GPU_ACCELERATION_NVIDIA
        if "amd" in vendors and detect_docker.docker_rocm_passthrough_works():
            return detect_docker.GPU_ACCELERATION_ROCM

        self._warn_gpu_unusable(gpu_label, vendors)
        return detect_docker.GPU_ACCELERATION_NONE

    def _warn_gpu_unusable(self, gpu_label: str, vendors: set[str]) -> None:
        """Explica *por qué* no se va a usar la GPU, que es distinto en cada
        caso. Un aviso genérico mandaba a revisar el controlador de NVIDIA a
        quien tenía una Intel, donde no hay nada que revisar."""
        if vendors == {"intel"}:
            why = (
                "Ollama no publica ninguna imagen para GPUs Intel, así que no hay "
                "forma de aprovecharla desde este stack."
            )
        elif "amd" in vendors:
            why = (
                "Docker no pudo abrir /dev/kfd y /dev/dri. Causas habituales: el "
                "kernel no trae el módulo `amdgpu`, no está instalado ROCm, o el "
                "usuario no pertenece a los grupos `video` y `render`."
            )
        else:
            why = (
                "Causas habituales: GPU sin soporte de passthrough bajo WSL2, "
                "controlador NVIDIA desactualizado, o falta "
                "`nvidia-container-toolkit`."
            )

        message = (
            f"Se detectó GPU ({gpu_label}) pero no se puede usar para la IA en este "
            f"equipo — el modelo correrá en CPU. {why}"
        )
        self.context.warn(message)
        console.print(f"[axion.warn]{message}[/]")

    # --- advertencias no fatales ---------------------------------------------------

    def _warn_about_crossed_filesystem(self, wsl: detect_platform.WslInfo) -> None:
        """§6.2: un `project_dir` en `/mnt/c/...` desde WSL funciona, pero el
        I/O cruzado es lento y los permisos POSIX no se preservan — justo los
        que `.env` y la clave del certificado necesitan."""
        if not wsl.inside_wsl:
            return
        if not str(self.context.project_dir).replace("\\", "/").startswith(WINDOWS_MOUNT_PREFIX):
            return
        message = (
            f"El proyecto está en {self.context.project_dir}, dentro del filesystem de "
            "Windows montado en WSL: el I/O es lento y los permisos POSIX de `.env` y "
            "`cert.key` no se preservan. Conviene moverlo al filesystem del WSL (~/)."
        )
        self.context.warn(message)
        console.print(f"[axion.warn]{message}[/]")

    def _warn_about_broken_mirrored(self, wsl: detect_platform.WslInfo) -> None:
        """Mirrored configurado pero con `eth0` de vuelta en `172.16/12`
        significa que no se aplicó: el stack no será visible en la LAN."""
        if not (wsl.inside_wsl and wsl.mirrored_configured):
            return
        from axion_wizard.detect import network as detect_network

        iface = detect_network.get_primary_interface()
        if iface is None or not detect_platform.is_eth0_in_forbidden_range(iface.ip):
            return
        message = (
            f"`.wslconfig` pide networkingMode=mirrored, pero la interfaz da {iface.ip} "
            "(rango interno de Docker Desktop): mirrored no está activo. El stack no "
            "será accesible desde la LAN sin `netsh portproxy`."
        )
        self.context.warn(message)
        console.print(f"[axion.warn]{message}[/]")

    def _warn_about_windows_docker_desktop_lan_exposure(
        self,
        os_info: detect_platform.OsInfo,
        wsl: detect_platform.WslInfo,
        docker: detect_docker.DockerInfo,
        variant: str,
    ) -> None:
        """El acceso LAN bajo Docker Desktop en Windows no es automático, y
        `axion-wizard.exe` casi siempre corre nativo en Windows (no dentro
        de WSL) — así que `_warn_about_broken_mirrored`, que exige
        `wsl.inside_wsl`, nunca llega a evaluarse en el caso más común.

        Se descubrió en vivo: un despliegue con Docker publicando los
        puertos correctamente y el firewall bien configurado seguía sin
        responder desde la LAN porque (a) mirrored networking no estaba
        realmente activo, o (b) la interfaz estaba categorizada "Public" en
        Windows, que aplica `BlockInbound` por defecto — ninguno de los dos
        aparece en ningún log de Docker ni de la propia app.
        """
        if wsl.inside_wsl or os_info.name != "Windows" or not docker.context.is_desktop:
            return
        if variant != WireguardVariant.PORTS.value:
            return

        wslconfig_path = detect_platform.locate_wslconfig_native()
        mirrored_configured = detect_platform.is_mirrored_networking_configured(wslconfig_path)

        if not mirrored_configured:
            message = (
                "Docker Desktop en Windows no expone sus puertos a la LAN por defecto: "
                "solo a `localhost`. Sin `networkingMode=mirrored` en "
                r"%UserProfile%\.wslconfig, es probable que Mattermost y el panel de "
                "WireGuard no respondan desde otros dispositivos de la red, aunque "
                "funcionen perfectamente en este equipo."
            )
            steps_hint = (
                "activar mirrored networking (recomendado) o configurar "
                "`netsh interface portproxy` + una regla de firewall apuntando a la IP "
                "de la LAN"
            )
        else:
            from axion_wizard.detect import network as detect_network

            iface = detect_network.get_primary_interface()
            category = detect_network.get_windows_network_category(
                iface.name if iface else None
            )
            if category != "Public":
                # Mirrored activo y red no-Public: la configuración de Windows
                # parece correcta. El aislamiento de clientes en el router es
                # harina de otro costal — se avisa igual, porque desde aquí
                # no hay forma de comprobarlo.
                self.context.warn(
                    "El acceso LAN depende también de que el router no aísle "
                    "clientes entre sí (AP/client isolation). Si Mattermost no carga "
                    "desde otro dispositivo pese a la configuración de Windows, "
                    "revisar esa opción en el panel del router."
                )
                self._warn_about_mirrored_tcp_stalls()
                return
            message = (
                f"La red de este equipo está categorizada \"Public\" en Windows "
                f"({iface.name if iface else 'interfaz principal'}), que bloquea el "
                "tráfico entrante por defecto — incluido el de mirrored networking, "
                "aunque esté activo. Mattermost y el panel de WireGuard probablemente "
                "no respondan desde otros dispositivos de la LAN."
            )
            steps_hint = (
                'reclasificar la red como "Privada": '
                "Set-NetConnectionProfile -NetworkCategory Private"
            )

        self.context.warn(message)
        console.print(f"[axion.warn]{message}[/]")
        console.print(f"[axion.dim]Antes de desplegar: {steps_hint}.[/]")

    def _warn_about_mirrored_tcp_stalls(self) -> None:
        """Mirrored networking arregla el acceso LAN y rompe otra cosa.

        `networkingMode=mirrored` es lo que hace que el stack sea alcanzable
        desde el móvil, pero arrastra un bug conocido y todavía abierto de
        Docker/WSL2 (moby/moby#48201): las conexiones TCP de larga vida se
        quedan colgadas. El WebSocket de Mattermost es exactamente eso —una
        conexión ociosa esperando a que llegue un mensaje— así que el
        síntoma no es un error, es que los mensajes nuevos (incluida la
        respuesta de la IA) solo aparecen al recargar la página.

        Se avisa aquí porque es indistinguible de "la IA no funciona" desde
        fuera, y porque el diagnóstico correcto está a un comando:
        `axion-wizard doctor` ahora hace el handshake WebSocket de verdad.
        """
        message = (
            "Mirrored networking está activo (es lo que da acceso desde la LAN), pero "
            "arrastra un bug abierto de WSL2/Docker con las conexiones TCP largas "
            "(moby/moby#48201). Si los mensajes —incluida la respuesta de la IA— solo "
            "aparecen al recargar con F5, es esto: el WebSocket de Mattermost se queda "
            "colgado. La alternativa es volver a NAT + `netsh portproxy`."
        )
        self.context.warn(message)
        console.print(f"[axion.dim]{message}[/]")
        console.print(
            "[axion.dim]Para confirmarlo sin tocar nada: `axion-wizard doctor` — "
            "la fila `WebSocket Mattermost` lo distingue de un problema de "
            "configuración.[/]"
        )

    # --- presentación -----------------------------------------------------------------

    @staticmethod
    def _render_table(facts: EnvironmentFacts) -> Table:
        table = ui.make_table("Entorno detectado")
        table.add_column("Elemento", style="axion.label")
        table.add_column("Valor", overflow="fold")

        table.add_row("Sistema operativo", f"{facts.os_info.name} {facts.os_info.release}")
        if facts.wsl.inside_wsl:
            distro = facts.wsl.distro_name or "?"
            version = facts.wsl.version or "?"
            table.add_row("WSL", f"{distro} (WSL{version})")
            mirrored = "[axion.ok]sí[/]" if facts.wsl.mirrored_configured else "[axion.dim]no[/]"
            table.add_row("Mirrored networking", mirrored)
        table.add_row("Docker", facts.docker.docker_version or "[axion.error]no detectado[/]")
        table.add_row("Compose", facts.docker.compose_version or "[axion.error]no detectado[/]")
        table.add_row("Contexto Docker", facts.docker.context.active_context or "por defecto")
        table.add_row("RAM", f"{facts.hardware.ram_total_gb:.1f} GB")
        table.add_row("CPU", f"{facts.hardware.cpu_logical} núcleos lógicos")
        gpus = ", ".join(g.name or g.vendor for g in facts.hardware.gpus)
        if not gpus:
            gpu_value = "[axion.dim]sin GPU dedicada[/]"
        elif facts.gpu_acceleration == detect_docker.GPU_ACCELERATION_NVIDIA:
            gpu_value = f"{gpus} [axion.ok]({ui.GLYPH_OK} CUDA vía Docker)[/]"
        elif facts.gpu_acceleration == detect_docker.GPU_ACCELERATION_ROCM:
            gpu_value = f"{gpus} [axion.ok]({ui.GLYPH_OK} ROCm vía Docker)[/]"
        else:
            gpu_value = f"{gpus} [axion.warn]({ui.GLYPH_WARN} sin passthrough, Ollama usará CPU)[/]"
        table.add_row("GPU", gpu_value)
        table.add_row("Variante WireGuard", f"[axion.info]{facts.wireguard_variant}[/]")
        return table
