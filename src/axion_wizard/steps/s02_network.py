"""Paso 2 — Verificaciones de red (§4.2).

Cuatro comprobaciones en el orden que exige la spec: IP LAN, CGNAT, puertos
libres y conectividad saliente. Ninguna aborta el flujo por sí sola —salvo
que el usuario decida parar—: son diagnósticos que el usuario necesita
*antes* de escribir nada al disco, no requisitos absolutos.

El CGNAT no se puede detectar solo: hace falta el IP WAN que el router
muestra en su panel, y no hay forma fiable de leerlo. Se le pide al usuario
y se compara (§4.2.2).
"""

from __future__ import annotations

import asyncio

from rich.table import Table

from axion_wizard import ui
from axion_wizard.console import console
from axion_wizard.detect import network as detect_network
from axion_wizard.errors import NetworkError
from axion_wizard.steps.base import Step, StepResult
from axion_wizard.steps.context import NetworkFacts
from axion_wizard.steps.prompts import interactive_input_available

#: Puertos que el stack necesita publicar. Ocupados no es fatal aquí —puede
#: ser el propio stack de una corrida anterior— pero sí hay que avisar.
CHECKED_PORTS = detect_network.REQUIRED_PORTS


class NetworkStep(Step):
    name = "network"
    title = "Verificaciones de red"

    def run(self) -> StepResult:
        facts = NetworkFacts()

        self._check_primary_interface(facts)
        self._check_ports(facts)
        asyncio.run(self._check_internet(facts))
        self._check_cgnat(facts)

        self.context.network = facts

        if not self.state.quiet:
            console.print(self._render_table(facts))

        if facts.unreachable_targets:
            self._confirm_or_abort(
                "Sin conectividad con "
                f"{', '.join(facts.unreachable_targets)}: el pull de imágenes y del "
                "modelo fallará más adelante.",
                question="¿Continuar de todos modos?",
            )

        return StepResult(
            name=self.name,
            ok=True,
            data={"lan_ip": facts.lan_ip or "", "cgnat": facts.cgnat},
            message=(
                f"IP LAN {facts.lan_ip or 'desconocida'}, "
                f"CGNAT: {'sí' if facts.cgnat else 'no'}"
            ),
        )

    def verify(self) -> StepResult:
        """Los puertos que el stack ya usa no cuentan como conflicto, así que
        verificar aquí solo confirma que seguimos teniendo IP de LAN."""
        iface = detect_network.get_primary_interface()
        if iface is None or not iface.ip:
            return StepResult(name=self.name, ok=False, message="sin interfaz con IP de LAN")
        return StepResult(name=self.name, ok=True, message=f"IP LAN {iface.ip}")

    # --- comprobaciones individuales ------------------------------------------------

    def _check_primary_interface(self, facts: NetworkFacts) -> None:
        iface = detect_network.get_primary_interface()
        if iface is None:
            self.context.warn("No se encontró ninguna interfaz con IP de LAN.")
            return
        facts.lan_ip = iface.ip
        facts.interface_name = iface.name

    def _check_ports(self, facts: NetworkFacts) -> None:
        """Nota crítica de §4.2: bajo Docker Desktop los contenedores publican
        sus puertos en otra VM, invisible para `psutil`. Se complementa con
        `docker ps` para no dar por libre un puerto que sí está tomado."""
        statuses = detect_network.check_ports_psutil(CHECKED_PORTS)
        statuses = detect_network.merge_docker_published_ports(statuses, _docker_ps_json())
        facts.busy_ports = [
            f"{s.port}/{s.protocol}" + (f" ({s.used_by})" if s.used_by else "")
            for s in statuses
            if s.in_use
        ]

    async def _check_internet(self, facts: NetworkFacts) -> None:
        facts.public_ip = await detect_network.get_public_ipv4()
        reachable = await detect_network.check_connectivity()
        facts.unreachable_targets = [name for name, ok in reachable.items() if not ok]

    def _check_cgnat(self, facts: NetworkFacts) -> None:
        """§4.2.2: comparar el IP público saliente con el WAN del router.

        Si difieren, la operadora hace Carrier-Grade NAT y ningún port
        forwarding alcanzará este host desde internet. No es motivo para
        abortar: el acceso por LAN y por VPN sigue funcionando.
        """
        if facts.public_ip is None:
            self.context.warn(
                "No se pudo determinar el IP público; no se comprobó el CGNAT."
            )
            return
        if self.state.yes or self.state.unattended or not interactive_input_available():
            # Sin interacción no hay con qué comparar: se deja sin determinar
            # en vez de inventarse un veredicto.
            return

        router_wan_ip = self._ask_router_wan_ip(facts.public_ip)
        if not router_wan_ip:
            return

        facts.cgnat = detect_network.is_cgnat(facts.public_ip, router_wan_ip)
        if not facts.cgnat:
            return

        console.print(
            f"[axion.warn]CGNAT detectado.[/] Tu IP pública ({facts.public_ip}) no coincide "
            f"con la WAN del router ({router_wan_ip}): la operadora usa Carrier-Grade NAT, "
            "así que el port forwarding nunca alcanzará este host desde internet."
        )
        self.context.warn(
            f"CGNAT detectado ({facts.public_ip} != {router_wan_ip}): solo acceso LAN/VPN."
        )
        self._confirm_or_abort(
            "El acceso desde fuera de la LAN no funcionará sin un relay WireGuard en un VPS.",
            question="¿Continuar solo con acceso LAN?",
        )

    def _ask_router_wan_ip(self, public_ip: str) -> str | None:
        import questionary

        console.print(
            f"[axion.info]IP pública detectada:[/] {public_ip}\n"
            "[axion.dim]Para descartar CGNAT hace falta la IP WAN que muestra el panel "
            "del router. Déjalo vacío para omitir esta comprobación.[/]"
        )
        answer = questionary.text("IP WAN del router (opcional):").ask()
        return (answer or "").strip() or None

    def _confirm_or_abort(self, explanation: str, question: str) -> None:
        console.print(f"[axion.warn]{explanation}[/]")
        if self.state.yes or self.state.unattended or not interactive_input_available():
            # Sin nadie a quien preguntar se continúa: el aviso ya está en
            # pantalla y abortar una instalación desatendida por un puerto
            # ocupado sería peor que seguir e informar al final.
            return

        import questionary

        if not questionary.confirm(question, default=True).ask():
            raise NetworkError(
                what="Instalación cancelada en las verificaciones de red",
                why=explanation,
                steps=[
                    "Corregir el problema de red y volver a ejecutar `axion-wizard install`.",
                    "El progreso hecho hasta aquí queda guardado y se reanuda solo.",
                ],
            )

    # --- presentación -------------------------------------------------------------------

    @staticmethod
    def _render_table(facts: NetworkFacts) -> Table:
        table = ui.make_table("Verificaciones de red")
        table.add_column("Comprobación", style="axion.label")
        table.add_column("Resultado")
        table.add_column("Detalle", overflow="fold")

        if facts.lan_ip:
            table.add_row(
                "Interfaz principal", ui.ok(), f"{facts.interface_name} — {facts.lan_ip}"
            )
        else:
            table.add_row("Interfaz principal", ui.fail(), "sin IP de LAN")

        table.add_row(
            "IP pública (IPv4)",
            ui.status(bool(facts.public_ip), fail_label="DESCONOCIDA"),
            facts.public_ip or "no se pudo determinar",
        )
        # Semántica invertida a propósito: aquí "SÍ" es el resultado
        # preocupante y "NO" el bueno — al revés que en el resto de filas.
        table.add_row(
            "CGNAT",
            ui.warn("SÍ") if facts.cgnat else ui.ok("NO"),
            "solo acceso LAN/VPN" if facts.cgnat else "",
        )
        table.add_row(
            "Puertos requeridos",
            ui.warn("OCUPADOS") if facts.busy_ports else ui.ok("LIBRES"),
            ", ".join(facts.busy_ports),
        )
        table.add_row(
            "Conectividad saliente",
            ui.status(not facts.unreachable_targets),
            ", ".join(facts.unreachable_targets),
        )
        return table


def _docker_ps_json() -> list[dict]:
    """`docker ps --format json`, o lista vacía si Docker no responde.

    Los puertos que publican los contenedores son un complemento al chequeo
    de `psutil`, no un requisito: si Docker no está listo todavía, el paso
    sigue siendo útil con lo que ve el host.

    El parseo va por `utils.jsonio` y no a mano: esta copia solo entendía el
    formato "un objeto por línea", así que con una CLI que emitiera un array
    JSON —lo que hacen algunas versiones— la comprobación de puertos
    ocupados bajo Docker Desktop devolvía "ningún contenedor" en silencio.
    """
    from axion_wizard.utils.jsonio import parse_json_lines_or_array
    from axion_wizard.utils.shell import CommandNotFoundError, CommandTimeoutError, run

    try:
        result = run(["docker", "ps", "--format", "json"], timeout=15.0)
    except (CommandNotFoundError, CommandTimeoutError):
        return []
    if not result.ok:
        return []
    return parse_json_lines_or_array(result.stdout)
