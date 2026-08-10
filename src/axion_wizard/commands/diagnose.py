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
    """Re-validate an already-deployed stack without modifying it (§4.9). It
    rebuilds host/model/variant by reading the artifacts in `--project-dir`,
    so it does not depend on an `install` having run in this same session."""
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
    """Just the network checks from §4.2."""
    from axion_wizard.detect import network as net

    table = ui.make_table("Network checks")
    table.add_column("Check", style="axion.label")
    table.add_column("Result")
    table.add_column("Detail", overflow="fold")

    iface = net.get_primary_interface()
    if iface is not None:
        table.add_row("Primary interface", ui.ok(), f"{iface.name} — {iface.ip}")
    else:
        table.add_row("Primary interface", ui.fail(), "no interface with a LAN IP")

    for port_status in net.check_ports_psutil():
        label = f"Port {port_status.port}/{port_status.protocol}"
        if port_status.free:
            table.add_row(label, ui.ok("FREE"), "")
        else:
            table.add_row(label, ui.warn("IN USE"), port_status.used_by or "")

    public_ip = asyncio.run(net.get_public_ipv4())
    table.add_row(
        "Public IP (IPv4)",
        ui.status(bool(public_ip), fail_label="UNKNOWN"),
        public_ip or "could not be determined",
    )

    for target, reachable in asyncio.run(net.check_connectivity()).items():
        table.add_row(
            f"Connectivity to {target}",
            ui.status(reachable),
            "" if reachable else "unreachable — pulling images/models will fail",
        )

    console.print(table)
    console.print(
        "[axion.dim]CGNAT: compare the public IP above with the router's WAN address "
        "(§4.2). If they differ, port forwarding will never work.[/]"
    )



def run_gen_cert(state: GlobalState, host: str) -> None:
    """Generate the TLS certificate and verify its SAN by reading it back (§4.4)."""
    from axion_wizard.services import certs

    cert_dir = state.project_dir / CERT_RELATIVE_DIR
    cert_path = cert_dir / "cert.crt"
    key_path = cert_dir / "cert.key"

    if state.dry_run:
        announce_dry_run(f"would generate {cert_path} and {key_path} with a SAN for {host!r}")
        return

    result = certs.generate_certificate(host, cert_path, key_path)
    # Read it back from the file itself; do not trust what we just built.
    san_entries = certs.verify_certificate_has_san(result.cert_path)

    console.print(f"[axion.ok]Certificate generated:[/] {result.cert_path}")
    console.print(f"[axion.ok]Private key:[/] {result.key_path} (permissions restricted)")
    console.print(f"[axion.info]SAN verified:[/] {', '.join(san_entries)}")

    _reload_nginx_certs(state)



def _reload_nginx_certs(state: GlobalState) -> None:
    """Make nginx re-read the freshly generated certificate.

    `nginx/certs` comes in through a bind mount, so the new file is already
    inside the container — but nginx loaded the previous one into memory at
    startup and will keep serving it indefinitely. Without this, `gen-cert`
    finished green while the browser went on seeing the old certificate, with
    nothing to explain why.
    """
    from axion_wizard.services import compose

    # The path is assembled by hand rather than via `compose_path_of`:
    # generating the certificate before a stack exists is legitimate — it
    # will be used when deploying — so a missing compose file is not an error
    # here.
    compose_path = state.project_dir / COMPOSE_FILENAME
    if not compose_path.exists():
        return

    status = compose.get_service_status(compose_path, NGINX_SERVICE)
    if status is None or not status.is_running:
        return

    if compose.restart(compose_path, NGINX_SERVICE).ok:
        console.print("[axion.ok]nginx restarted:[/] it now serves the new certificate.")
    else:
        console.print(
            "[axion.warn]Could not restart nginx[/], so it will keep serving the "
            "previous certificate. Apply it with: axion-wizard up nginx"
        )
