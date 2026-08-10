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
    """Crea un cliente en el panel wg-easy y muestra su QR en la terminal (§4.8).

    Las credenciales salen de `wg.env`, no de un prompt. Con wg-easy v14 no
    había alternativa —allí solo se guardaba el hash bcrypt— y este comando
    pedía la contraseña cada vez, incluso ejecutándose desde el mismo
    directorio que la tiene escrita. La v15 la quiere en claro, así que ya
    está ahí; solo se pregunta si de verdad falta.
    """
    from axion_wizard.domain.deployment import discover_deployment, env_value
    from axion_wizard.services import wireguard as wg

    facts = discover_deployment(state.project_dir)
    panel_url = wg.build_panel_url(facts.host)

    if state.dry_run:
        announce_dry_run(f"crearía el cliente {name!r} en {panel_url}")
        return

    console.print(f"[axion.info]Panel WireGuard:[/] {panel_url}")
    console.print(f"[axion.warn]{wg.PANEL_HTTPS_WARNING}[/]")

    username = env_value(state.project_dir, "INIT_USERNAME", filename="wg.env")
    password = env_value(state.project_dir, "INIT_PASSWORD", filename="wg.env")
    if not (username and password):
        username, password = _ask_panel_credentials(username)

    async def _create() -> wg.WireguardClient:
        await wg.wait_for_panel_ready(panel_url)
        async with wg.WireguardPanelClient(panel_url) as panel:
            await panel.login(username, password)
            return await wg.create_client_with_qr(panel, name)

    client = asyncio.run(_create())

    console.print(f"\n[axion.ok]Cliente creado:[/] {client.name} (id {client.id})\n")
    console.print(wg.render_qr_terminal(client.config_text))
    console.print(
        "[axion.dim]Escanea el QR con la app de WireGuard, o importa la "
        "configuración manualmente desde el panel.[/]"
    )


def _ask_panel_credentials(known_username: str | None) -> tuple[str, str]:
    """Pregunta lo que falte en `wg.env` para poder entrar al panel.

    Se llega aquí cuando el archivo no está o le faltan las claves: un
    proyecto movido de sitio, un `wg.env` editado a mano, o un despliegue
    hecho por otros medios. Preguntar es mejor que fallar, pero no es el
    camino normal.
    """
    from axion_wizard.steps.prompts import require_interactive_input

    require_interactive_input("Dar de alta un cliente de WireGuard")

    import questionary

    username = known_username or (
        questionary.text("Usuario del panel WireGuard:", default="admin").ask() or ""
    ).strip()
    password = questionary.password("Contraseña del panel WireGuard:").ask()
    if not (username and password):
        raise ConfigError(
            what="Faltan las credenciales del panel WireGuard",
            why="Sin autenticarse contra wg-easy no se puede crear el cliente.",
            steps=[
                "Reintentar con el usuario y la contraseña configurados en el paso 3.",
                "Comprobar que wg.env tiene INIT_USERNAME e INIT_PASSWORD.",
            ],
        )
    return username, password
