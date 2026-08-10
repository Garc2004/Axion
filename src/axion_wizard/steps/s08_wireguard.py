"""Paso 8 — WireGuard: primer cliente y QR en terminal (§4.8).

El panel de wg-easy sirve **HTTP puro, no HTTPS**: abrirlo con `https://` da
`ERR_SSL_PROTOCOL_ERROR`. La URL se construye siempre con `http://`
explícito y se advierte en pantalla, porque es el error que todo el mundo
comete al ver que el resto del stack va por TLS.

El QR se dibuja con caracteres de bloque Unicode en la propia terminal: el
usuario escanea desde el móvil sin tener que abrir el panel en un navegador.

Este paso ya no pregunta nada. Con wg-easy v14 tenía que volver a pedir la
contraseña del panel a mitad de la instalación —lo único guardado era su
hash bcrypt, y de un hash no se saca la contraseña—, así que el usuario la
escribía dos veces en la misma ejecución sin ningún motivo visible. La v15
la quiere en claro, o sea que ya está en `AxionConfig` y en `wg.env`, y el
cliente se crea solo.
"""

from __future__ import annotations

import asyncio

from axion_wizard.errors import AxionError
from axion_wizard.render.console import console
from axion_wizard.services import wireguard as wg
from axion_wizard.steps.base import Step, StepResult

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

        try:
            client = asyncio.run(
                self._create_client(
                    panel_url,
                    config.wireguard_admin_username,
                    config.wireguard_admin_password.get_secret_value(),
                )
            )
        except AxionError as exc:
            # No es un fallo del despliegue: el stack está levantado y el
            # cliente se puede crear luego con `wireguard add-client`. Antes
            # este camino solo se daba si el usuario dejaba el prompt vacío;
            # ahora cubre además que el panel no responda o rechace las
            # credenciales, que es donde de verdad conviene no tirar abajo
            # una instalación que por lo demás terminó bien.
            message = (
                f"No se pudo crear el cliente WireGuard inicial ({exc}). "
                "Créalo cuando quieras con: axion-wizard wireguard add-client <nombre>"
            )
            self.context.warn(message)
            console.print(f"[axion.warn]{message}[/]")
            return StepResult(name=self.name, ok=True, message="sin cliente inicial")

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

    @staticmethod
    async def _create_client(
        panel_url: str, username: str, password: str
    ) -> wg.WireguardClient:
        await wg.wait_for_panel_ready(panel_url)
        async with wg.WireguardPanelClient(panel_url) as panel:
            await panel.login(username, password)
            return await wg.create_client_with_qr(panel, DEFAULT_CLIENT_NAME)
