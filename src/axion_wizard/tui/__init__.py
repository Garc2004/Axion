"""Interfaz a pantalla completa (Textual) para el flujo de instalación.

**Alternativa, no reemplazo.** La §1.3 de la spec descarta Textual para el
flujo lineal de instalación, y esa decisión sigue en pie: `axion-wizard
install` usa questionary y Rich como siempre. Esto es lo que se ejecuta solo
con `install --tui`.

El diseño evita el conflicto de fondo entre ambas cosas —questionary y
Textual se pelean por la terminal— recogiendo *toda* la configuración en un
formulario antes de arrancar, y ejecutando después los diez pasos en modo
desatendido: así ningún paso intenta abrir un prompt mientras Textual tiene
el control de la pantalla. El paso del bot/webhook no tiene formulario propio
aquí — en modo desatendido se omite sin más, y se aplica después con
`set-bot-token`/`set-webhook-token` o `doctor`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from axion_wizard.cli import GlobalState


def run_tui_install(state: GlobalState) -> bool:
    """Arranca la TUI y devuelve si la instalación terminó bien.

    El import va dentro para que Textual —que arrastra su propio árbol de
    dependencias— no se cargue en un `axion-wizard --version`.
    """
    from axion_wizard.tui.app import AxionInstallerApp

    app = AxionInstallerApp(state)
    app.run()
    return app.succeeded
