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
    """Forget the saved progress so `install` starts again at step 1.

    It deletes only `.axion-wizard-state.json`: no containers, no volumes, no
    `.env`, no certificate. That is deliberate — "I want to redo the steps"
    and "I want to delete my data" are different things, and the second is
    what `uninstall --purge` is for. Because step 3 reuses the PostgreSQL
    password already in `.env`, redoing the install over an existing
    deployment remains safe.
    """
    from axion_wizard.utils import state as state_store

    path = state_store.state_path(state.project_dir)
    if not path.exists():
        console.print(
            "[axion.info]There is no saved progress:[/] the next install would "
            "already start at step 1."
        )
        return

    previous = state_store.load_state(state.project_dir)
    done = [s for s in previous.completed_steps if s.ok]
    console.print(
        f"[axion.warn]Progress for {len(done)} of "
        f"{len(previous.completed_steps)} recorded steps will be discarded[/] in {path}."
    )
    console.print(
        "[axion.dim]Nothing else is deleted: containers, volumes, `.env` and the "
        "certificate stay as they are. To delete the data: "
        "axion-wizard uninstall --purge[/]"
    )

    if state.dry_run:
        announce_dry_run(f"would delete {path}")
        return

    if not (yes or state.yes):
        import questionary

        from axion_wizard.steps.prompts import interactive_input_available

        if interactive_input_available() and not questionary.confirm(
            "Start the install from scratch?", default=True
        ).ask():
            console.print("[axion.warn]Cancelled; the progress was left alone.[/]")
            raise typer.Exit(code=1)

    path.unlink()
    console.print(
        "[axion.ok]Progress cleared.[/] The next run of `axion-wizard install` "
        "will start at step 1."
    )



def run_install(
    state: GlobalState,
    unattended: bool = False,
    config_path: Path | None = None,
    tui: bool = False,
    restart: bool = False,
) -> None:
    """The complete install flow (§4).

    `install`'s own options travel through `GlobalState` rather than being
    threaded by signature down to every step: there are ten steps and only
    three of them consult these.
    """
    from axion_wizard.steps import orchestrator

    state.unattended = unattended
    state.config_path = config_path

    if restart:
        # `--restart` is `reset` + `install` in one command, without asking
        # for confirmation: asking twice for an intent that was already
        # explicit is one time too many.
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
    """The TUI needs an interactive terminal and a form to fill in.

    Combining it with `--unattended` or with redirected output produces no
    obvious error on its own: Textual would start and sit waiting for
    keystrokes that never arrive, which from the outside looks like a hang.
    """
    import sys

    if unattended:
        raise ConfigError(
            what="`--tui` and `--unattended` are mutually exclusive",
            why="The full-screen interface exists to fill in a form by hand.",
            steps=[
                "For CI: axion-wizard install --unattended --config axion.toml",
                "For interactive use: axion-wizard install --tui",
            ],
        )
    if not (sys.stdin and sys.stdin.isatty()):
        raise ConfigError(
            what="`--tui` needs an interactive terminal",
            why="Standard input is not a TTY, so the form would receive no keystrokes.",
            steps=[
                "Run it directly in a terminal, with no pipes or redirections.",
                "Or use the normal flow: axion-wizard install",
            ],
        )
