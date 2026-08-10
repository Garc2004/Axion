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
    """Enrol a client in the wg-easy panel and show its QR in the terminal (§4.8).

    The credentials come from `wg.env`, not from a prompt. Under wg-easy v14
    there was no alternative — only the bcrypt hash was stored — and this
    command asked for the password every time, even when run from the very
    directory that had it written down. v15 wants it in the clear, so it is
    already there; it only asks if it genuinely is not.
    """
    from axion_wizard.domain.deployment import discover_deployment, env_value
    from axion_wizard.services import wireguard as wg

    facts = discover_deployment(state.project_dir)
    panel_url = wg.build_panel_url(facts.host)

    if state.dry_run:
        announce_dry_run(f"would create client {name!r} at {panel_url}")
        return

    console.print(f"[axion.info]WireGuard panel:[/] {panel_url}")
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

    console.print(f"\n[axion.ok]Client created:[/] {client.name} (id {client.id})\n")
    console.print(wg.render_qr_terminal(client.config_text))
    console.print(
        "[axion.dim]Scan the QR with the WireGuard app, or import the "
        "configuration manually from the panel.[/]"
    )


def _ask_panel_credentials(known_username: str | None) -> tuple[str, str]:
    """Ask for whatever `wg.env` is missing in order to get into the panel.

    This is reached when the file is absent or the keys are missing: a project
    moved elsewhere, a hand-edited `wg.env`, or a deployment made by other
    means. Asking beats failing, but it is not the normal path.
    """
    from axion_wizard.steps.prompts import require_interactive_input

    require_interactive_input("Enrolling a WireGuard client")

    import questionary

    username = known_username or (
        questionary.text("WireGuard panel username:", default="admin").ask() or ""
    ).strip()
    password = questionary.password("WireGuard panel password:").ask()
    if not (username and password):
        raise ConfigError(
            what="The WireGuard panel credentials are missing",
            why="Without authenticating against wg-easy the client cannot be created.",
            steps=[
                "Retry with the username and password configured in step 3.",
                "Check that wg.env carries INIT_USERNAME and INIT_PASSWORD.",
            ],
        )
    return username, password
