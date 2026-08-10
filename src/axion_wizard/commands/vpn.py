"""`wireguard add-client` — enrolling a peer against the wg-easy panel."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from axion_wizard.commands._common import announce_dry_run
from axion_wizard.errors import ConfigError
from axion_wizard.render.console import console

if TYPE_CHECKING:
    from axion_wizard.cli import GlobalState



def run_wireguard_add_client(state: GlobalState, name: str) -> None:
    """Crea un cliente en el panel wg-easy y muestra su QR en la terminal (§4.8)."""
    import questionary

    from axion_wizard.domain.deployment import discover_deployment
    from axion_wizard.services import wireguard as wg

    facts = discover_deployment(state.project_dir)
    panel_url = wg.build_panel_url(facts.host)

    if state.dry_run:
        announce_dry_run(f"crearía el cliente {name!r} en {panel_url}")
        return

    console.print(f"[axion.info]Panel WireGuard:[/] {panel_url}")
    console.print(f"[axion.warn]{wg.PANEL_HTTPS_WARNING}[/]")

    password = questionary.password("Contraseña del panel WireGuard:").ask()
    if not password:
        raise ConfigError(
            what="No se introdujo la contraseña del panel",
            why="Sin autenticarse contra wg-easy no se puede crear el cliente.",
            steps=["Reintentar introduciendo la contraseña configurada en el paso 3."],
        )

    async def _create() -> wg.WireguardClient:
        await wg.wait_for_panel_ready(panel_url)
        async with wg.WireguardPanelClient(panel_url) as panel:
            await panel.login(password)
            return await wg.create_client_with_qr(panel, name)

    client = asyncio.run(_create())

    console.print(f"\n[axion.ok]Cliente creado:[/] {client.name} (id {client.id})\n")
    console.print(wg.render_qr_terminal(client.config_text))
    console.print(
        "[axion.dim]Escanea el QR con la app de WireGuard, o importa la "
        "configuración manualmente desde el panel.[/]"
    )
