"""`doctor`, `network-check` and `gen-cert` — looking without touching.

`gen-cert` is the one exception: it writes the certificate and then asks
nginx to reload it, because a certificate nginx never re-reads is a
certificate that did not change anything.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import typer

from axion_wizard.commands._common import CERT_RELATIVE_DIR, COMPOSE_FILENAME, announce_dry_run
from axion_wizard.domain.stack import NGINX_SERVICE
from axion_wizard.render import ui
from axion_wizard.render.console import console

if TYPE_CHECKING:
    from axion_wizard.cli import GlobalState



def run_doctor(state: GlobalState) -> None:
    """Re-valida un stack ya desplegado sin modificarlo (§4.9). Reconstruye
    host/modelo/variante leyendo los artefactos en `--project-dir`, no
    depende de una corrida previa de `install` en esta misma sesión."""
    from axion_wizard.domain.deployment import discover_deployment
    from axion_wizard.steps.s09_verify import (
        all_checks_passed,
        render_checks_table,
        run_all_checks,
    )

    facts = discover_deployment(state.project_dir)
    results = asyncio.run(run_all_checks(facts))
    console.print(render_checks_table(results))
    if not all_checks_passed(results):
        raise typer.Exit(code=1)



def run_network_check(state: GlobalState) -> None:
    """Solo las verificaciones de red de §4.2."""
    from axion_wizard.detect import network as net

    table = ui.make_table("Verificaciones de red")
    table.add_column("Comprobación", style="axion.label")
    table.add_column("Resultado")
    table.add_column("Detalle", overflow="fold")

    iface = net.get_primary_interface()
    if iface is not None:
        table.add_row("Interfaz principal", ui.ok(), f"{iface.name} — {iface.ip}")
    else:
        table.add_row("Interfaz principal", ui.fail(), "sin interfaz con IP de LAN")

    for port_status in net.check_ports_psutil():
        label = f"Puerto {port_status.port}/{port_status.protocol}"
        if port_status.free:
            table.add_row(label, ui.ok("LIBRE"), "")
        else:
            table.add_row(label, ui.warn("OCUPADO"), port_status.used_by or "")

    public_ip = asyncio.run(net.get_public_ipv4())
    table.add_row(
        "IP pública (IPv4)",
        ui.status(bool(public_ip), fail_label="DESCONOCIDA"),
        public_ip or "no se pudo determinar",
    )

    for target, reachable in asyncio.run(net.check_connectivity()).items():
        table.add_row(
            f"Conectividad {target}",
            ui.status(reachable),
            "" if reachable else "inalcanzable — el pull de imágenes/modelos fallará",
        )

    console.print(table)
    console.print(
        "[axion.dim]CGNAT: comparar la IP pública de arriba con la WAN del router "
        "(§4.2). Si no coinciden, el port forwarding no funcionará.[/]"
    )



def run_gen_cert(state: GlobalState, host: str) -> None:
    """Genera el certificado TLS y verifica su SAN leyéndolo de vuelta (§4.4)."""
    from axion_wizard.services import certs

    cert_dir = state.project_dir / CERT_RELATIVE_DIR
    cert_path = cert_dir / "cert.crt"
    key_path = cert_dir / "cert.key"

    if state.dry_run:
        announce_dry_run(f"generaría {cert_path} y {key_path} con SAN para {host!r}")
        return

    result = certs.generate_certificate(host, cert_path, key_path)
    # Releer del propio archivo, no confiar en lo que acabamos de construir.
    san_entries = certs.verify_certificate_has_san(result.cert_path)

    console.print(f"[axion.ok]Certificado generado:[/] {result.cert_path}")
    console.print(f"[axion.ok]Clave privada:[/] {result.key_path} (permisos restringidos)")
    console.print(f"[axion.info]SAN verificado:[/] {', '.join(san_entries)}")

    _reload_nginx_certs(state)



def _reload_nginx_certs(state: GlobalState) -> None:
    """Hace que nginx relea el certificado recién generado.

    `nginx/certs` entra por bind mount, así que el archivo nuevo ya está
    dentro del contenedor — pero nginx cargó el anterior en memoria al
    arrancar y lo seguirá sirviendo indefinidamente. Sin esto, `gen-cert`
    terminaba en verde y el navegador seguía viendo el certificado viejo, sin
    nada que explicara por qué.
    """
    from axion_wizard.services import compose

    # La ruta se arma a mano en vez de con `_compose_path`: generar el
    # certificado antes de que exista un stack es legítimo —se usará al
    # desplegar—, así que aquí la ausencia del compose no es un error.
    compose_path = state.project_dir / COMPOSE_FILENAME
    if not compose_path.exists():
        return

    status = compose.get_service_status(compose_path, NGINX_SERVICE)
    if status is None or not status.is_running:
        return

    if compose.restart(compose_path, NGINX_SERVICE).ok:
        console.print("[axion.ok]nginx reiniciado:[/] ya sirve el certificado nuevo.")
    else:
        console.print(
            "[axion.warn]No se pudo reiniciar nginx[/], que seguirá sirviendo el "
            "certificado anterior. Aplícalo con: axion-wizard up nginx"
        )
