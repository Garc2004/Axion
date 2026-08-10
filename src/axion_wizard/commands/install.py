"""`install` and `reset` — running (or re-running) the install flow."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import typer

from axion_wizard.commands._common import announce_dry_run
from axion_wizard.errors import ConfigError
from axion_wizard.render.console import console

if TYPE_CHECKING:
    from axion_wizard.cli import GlobalState



def run_reset(state: GlobalState, yes: bool = False) -> None:
    """Olvida el progreso guardado para que `install` empiece por el paso 1.

    Solo borra `.axion-wizard-state.json`: ni contenedores, ni volúmenes, ni
    `.env`, ni el certificado. Es deliberado — "quiero rehacer los pasos" y
    "quiero borrar mis datos" son cosas distintas, y para la segunda está
    `uninstall --purge`. Como el paso 3 reutiliza la contraseña de PostgreSQL
    que ya está en `.env`, rehacer la instalación sobre un despliegue
    existente sigue siendo seguro.
    """
    from axion_wizard.utils import state as state_store

    path = state_store.state_path(state.project_dir)
    if not path.exists():
        console.print(
            "[axion.info]No hay progreso guardado:[/] la próxima instalación ya "
            "empezaría por el paso 1."
        )
        return

    previous = state_store.load_state(state.project_dir)
    done = [s for s in previous.completed_steps if s.ok]
    console.print(
        f"[axion.warn]Se descartará el progreso de {len(done)} de "
        f"{len(previous.completed_steps)} pasos registrados[/] en {path}."
    )
    console.print(
        "[axion.dim]No se borra nada más: contenedores, volúmenes, `.env` y el "
        "certificado se quedan como están. Para borrar los datos: "
        "axion-wizard uninstall --purge[/]"
    )

    if state.dry_run:
        announce_dry_run(f"borraría {path}")
        return

    if not (yes or state.yes):
        import questionary

        from axion_wizard.steps.prompts import interactive_input_available

        if interactive_input_available() and not questionary.confirm(
            "¿Empezar la instalación de cero?", default=True
        ).ask():
            console.print("[axion.warn]Cancelado; no se tocó el progreso.[/]")
            raise typer.Exit(code=1)

    path.unlink()
    console.print(
        "[axion.ok]Progreso borrado.[/] La próxima ejecución de `axion-wizard install` "
        "empezará por el paso 1."
    )



def run_install(
    state: GlobalState,
    unattended: bool = False,
    config_path: Path | None = None,
    tui: bool = False,
    restart: bool = False,
) -> None:
    """Flujo completo de instalación (§4).

    Las opciones propias de `install` se pasan por `GlobalState` en vez de
    encadenarlas por firma hasta cada paso: son diez pasos y solo tres las
    consultan.
    """
    from axion_wizard.steps import orchestrator

    state.unattended = unattended
    state.config_path = config_path

    if restart:
        # `--restart` es `reset` + `install` en un solo comando, sin pedir
        # confirmación: pedirla dos veces para una intención ya explícita
        # sobra.
        run_reset(state, yes=True)

    if tui:
        _assert_tui_is_usable(state, unattended)
        from axion_wizard.tui import run_tui_install

        if not run_tui_install(state):
            raise typer.Exit(code=1)
        return

    if not orchestrator.install(state):
        raise typer.Exit(code=1)



def _assert_tui_is_usable(state: GlobalState, unattended: bool) -> None:
    """La TUI necesita una terminal interactiva y un formulario que rellenar.

    Combinarla con `--unattended` o con la salida redirigida no da un error
    obvio por sí solo: Textual arrancaría y se quedaría esperando teclas que
    nunca llegan, que desde fuera parece un cuelgue.
    """
    import sys

    if unattended:
        raise ConfigError(
            what="`--tui` y `--unattended` se excluyen",
            why="La interfaz a pantalla completa existe para rellenar un formulario a mano.",
            steps=[
                "Para CI: axion-wizard install --unattended --config axion.toml",
                "Para uso interactivo: axion-wizard install --tui",
            ],
        )
    if not (sys.stdin and sys.stdin.isatty()):
        raise ConfigError(
            what="`--tui` necesita una terminal interactiva",
            why="La entrada estándar no es una TTY, así que el formulario no recibiría teclas.",
            steps=[
                "Ejecutarlo directamente en una terminal, sin tuberías ni redirecciones.",
                "O usar el flujo normal: axion-wizard install",
            ],
        )
