"""Paso 8 — WireGuard: primer cliente y QR en terminal (§4.8).

El panel de wg-easy v14 sirve **HTTP puro, no HTTPS**: abrirlo con `https://`
da `ERR_SSL_PROTOCOL_ERROR`. La URL se construye siempre con `http://`
explícito y se advierte en pantalla, porque es el error que todo el mundo
comete al ver que el resto del stack va por TLS.

El QR se dibuja con caracteres de bloque Unicode en la propia terminal: el
usuario escanea desde el móvil sin tener que abrir el panel en un navegador.
"""

from __future__ import annotations

import asyncio

from axion_wizard.render.console import console
from axion_wizard.services import wireguard as wg
from axion_wizard.steps.base import Step, StepResult
from axion_wizard.steps.prompts import interactive_input_available

DEFAULT_CLIENT_NAME = "primer-cliente"


class WireguardStep(Step):
    name = "wireguard"
    title = "Cliente WireGuard"
    #: No se revalida al reanudar: `verify()` espera hasta 30s a que el panel
    #: responda, y ningún paso posterior depende de que exista un cliente —
    #: es el penúltimo y lo único que viene detrás es el informe final. Pagar
    #: esa espera en cada reanudación no protegería nada.
    revalidate_on_resume = False

    def run(self) -> StepResult:
        config = self.context.require_config()
        panel_url = wg.build_panel_url(config.host)

        if self.state.dry_run:
            console.print(
                f"[axion.info][dry-run][/] crearía el cliente {DEFAULT_CLIENT_NAME!r} "
                f"en {panel_url}"
            )
            return StepResult(name=self.name, ok=True, message="omitido por --dry-run")

        console.print(f"[axion.info]Panel WireGuard:[/] {panel_url}")
        console.print(f"[axion.warn]{wg.PANEL_HTTPS_WARNING}[/]")

        password = self._ask_password()
        if password is None:
            # No es un fallo del despliegue: el stack está levantado y el
            # cliente se puede crear luego con `wireguard add-client`.
            message = (
                "Sin cliente WireGuard inicial. Créalo cuando quieras con: "
                "axion-wizard wireguard add-client <nombre>"
            )
            self.context.warn(message)
            console.print(f"[axion.warn]{message}[/]")
            return StepResult(name=self.name, ok=True, message="omitido por el usuario")

        client = asyncio.run(self._create_client(panel_url, password))

        console.print(f"\n[axion.ok]Cliente creado:[/] {client.name} (id {client.id})\n")
        console.print(wg.render_qr_terminal(client.config_text))
        console.print(
            "[axion.dim]Escanea el QR con la app de WireGuard, o importa la "
            "configuración manualmente desde el panel.[/]"
        )
        return StepResult(name=self.name, ok=True, message=f"cliente {client.name} creado")

    def verify(self) -> StepResult:
        """Solo comprueba que el panel responde: cuántos clientes haya es cosa
        del usuario, no una condición de éxito del despliegue."""
        if self.state.dry_run:
            return StepResult(name=self.name, ok=True, message="omitido por --dry-run")

        config = self.context.require_config()
        panel_url = wg.build_panel_url(config.host)
        try:
            asyncio.run(wg.wait_for_panel_ready(panel_url, timeout=30.0))
        except Exception as exc:  # noqa: BLE001 - se reporta como paso no verificado
            return StepResult(name=self.name, ok=False, message=str(exc))
        return StepResult(name=self.name, ok=True, message=f"panel operativo en {panel_url}")

    def _ask_password(self) -> str | None:
        """La contraseña del panel es la que el usuario eligió en el paso 3,
        pero no se reutiliza desde memoria a propósito: lo que se guardó es su
        hash bcrypt (§9), y de un hash no se saca la contraseña."""
        if self.state.unattended or not interactive_input_available():
            return None

        import questionary

        answer = questionary.password(
            "Contraseña del panel WireGuard (la del paso 3, vacío para omitir):"
        ).ask()
        return (answer or "").strip() or None

    @staticmethod
    async def _create_client(panel_url: str, password: str) -> wg.WireguardClient:
        await wg.wait_for_panel_ready(panel_url)
        async with wg.WireguardPanelClient(panel_url) as panel:
            await panel.login(password)
            return await wg.create_client_with_qr(panel, DEFAULT_CLIENT_NAME)
