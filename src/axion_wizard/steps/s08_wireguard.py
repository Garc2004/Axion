"""Step 8 — WireGuard: first client and QR in the terminal (§4.8).

The wg-easy panel serves **plain HTTP, not HTTPS**: opening it with
`https://` gives `ERR_SSL_PROTOCOL_ERROR`. The URL is always built with an
explicit `http://` and warned about on screen, because it is the mistake
everyone makes on seeing that the rest of the stack runs over TLS.

The QR is drawn with Unicode block characters in the terminal itself: the
user scans it from their phone without having to open the panel in a browser.

This step no longer asks anything. Under wg-easy v14 it had to ask for the
panel password again halfway through the install — all that was stored was
its bcrypt hash, and a hash does not give the password back — so the user
typed it twice in a single run for no visible reason. v15 wants it in the
clear, meaning it is already in `AxionConfig` and in `wg.env`, and the client
is created without asking.
"""

from __future__ import annotations

import asyncio

from axion_wizard.errors import AxionError
from axion_wizard.render.console import console
from axion_wizard.services import wireguard as wg
from axion_wizard.steps.base import Step, StepResult

DEFAULT_CLIENT_NAME = "first-client"


class WireguardStep(Step):
    name = "wireguard"
    title = "WireGuard client"
    #: Not revalidated on resume: `verify()` waits up to 30s for the panel to
    #: answer, and no later step depends on a client existing — this is the
    #: second-to-last step and all that follows is the final report. Paying
    #: that wait on every resume would protect nothing.
    revalidate_on_resume = False

    def run(self) -> StepResult:
        config = self.context.require_config()
        panel_url = wg.build_panel_url(config.host)

        if self.state.dry_run:
            console.print(
                f"[axion.info][dry-run][/] would create client {DEFAULT_CLIENT_NAME!r} "
                f"at {panel_url}"
            )
            return StepResult(name=self.name, ok=True, message="skipped by --dry-run")

        console.print(f"[axion.info]WireGuard panel:[/] {panel_url}")
        console.print(f"[axion.warn]{wg.PANEL_HTTPS_WARNING}[/]")

        try:
            client = asyncio.run(
                self._create_client(
                    panel_url,
                    config.wireguard_admin_username,
                    config.wireguard_admin_password.get_secret_value(),
                )
            )
        except AxionError as exc:
            # Not a deployment failure: the stack is up and the client can be
            # created later with `wireguard add-client`. This path used to be
            # reached only when the user left the prompt empty; it now also
            # covers the panel not answering or rejecting the credentials,
            # which is where it really matters not to tear down an install
            # that otherwise finished fine.
            message = (
                f"Could not create the initial WireGuard client ({exc}). "
                "Create one whenever you like with: axion-wizard wireguard add-client <name>"
            )
            self.context.warn(message)
            console.print(f"[axion.warn]{message}[/]")
            return StepResult(name=self.name, ok=True, message="no initial client")

        console.print(f"\n[axion.ok]Client created:[/] {client.name} (id {client.id})\n")
        console.print(wg.render_qr_terminal(client.config_text))
        console.print(
            "[axion.dim]Scan the QR with the WireGuard app, or import the "
            "configuration manually from the panel.[/]"
        )
        return StepResult(name=self.name, ok=True, message=f"client {client.name} created")

    def verify(self) -> StepResult:
        """Only checks that the panel answers: how many clients there are is
        the user's business, not a success condition for the deployment."""
        if self.state.dry_run:
            return StepResult(name=self.name, ok=True, message="skipped by --dry-run")

        config = self.context.require_config()
        panel_url = wg.build_panel_url(config.host)
        try:
            asyncio.run(wg.wait_for_panel_ready(panel_url, timeout=30.0))
        except Exception as exc:  # noqa: BLE001 - reported as an unverified step
            return StepResult(name=self.name, ok=False, message=str(exc))
        return StepResult(name=self.name, ok=True, message=f"panel live at {panel_url}")

    @staticmethod
    async def _create_client(
        panel_url: str, username: str, password: str
    ) -> wg.WireguardClient:
        await wg.wait_for_panel_ready(panel_url)
        async with wg.WireguardPanelClient(panel_url) as panel:
            await panel.login(username, password)
            return await wg.create_client_with_qr(panel, DEFAULT_CLIENT_NAME)
