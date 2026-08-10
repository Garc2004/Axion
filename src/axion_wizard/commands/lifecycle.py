"""`up`, `down`, `logs` and `uninstall` — the stack's on/off switches."""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

from axion_wizard.commands._common import announce_dry_run, compose_path_of
from axion_wizard.domain.stack import MANAGED_SERVICES, WIREGUARD_SERVICE
from axion_wizard.render.console import console

if TYPE_CHECKING:
    from axion_wizard.cli import GlobalState



def run_compose_up(state: GlobalState, service: str | None = None) -> None:
    from axion_wizard.services import compose
    from axion_wizard.steps import s06_deploy

    compose_path = compose_path_of(state)

    if state.dry_run:
        target = service or "every service"
        announce_dry_run(f"would run `docker compose up -d --build` for {target}")
        return

    services = [service] if service else list(MANAGED_SERVICES)
    s06_deploy.deploy(compose_path, services=services)

    # §6.4 requires checking the container's *effective* tag, not just the one
    # left written in the compose file: anyone hand-editing it can end up
    # running another wg-easy major, which configures itself incompatibly and
    # leaves the panel unusable without a single error in the logs.
    if WIREGUARD_SERVICE in services:
        s06_deploy.verify_wg_easy_tag(compose_path)

    # `axion-wizard up mattermost` recreates the container on a different IP
    # and leaves nginx pointing at the old one: 502 across the whole stack
    # without any healthcheck noticing.
    if s06_deploy.refresh_nginx(compose_path, services):
        console.print("[axion.ok]nginx restarted[/] so it can find Mattermost again.")

    console.print("[axion.ok]Stack is up.[/]")

    for status in compose.ps(compose_path):
        health = f" ({status.health})" if status.health else ""
        console.print(f"  {status.service}: {status.state}{health}")



def run_compose_down(state: GlobalState) -> None:
    from axion_wizard.services import compose

    compose_path = compose_path_of(state)

    if state.dry_run:
        announce_dry_run("would run `docker compose down` (keeping the volumes)")
        return

    result = compose.down(compose_path)
    if result.ok:
        console.print("[axion.ok]Stack stopped.[/] The data volumes are kept.")
    else:
        console.print(f"[axion.error]`docker compose down` failed:[/] {result.stderr.strip()}")
        raise typer.Exit(code=1)



def run_compose_logs(state: GlobalState, service: str | None = None) -> None:
    from axion_wizard.services import compose

    compose_path = compose_path_of(state)
    targets = [service] if service else list(MANAGED_SERVICES)

    printed_any = False
    for name in targets:
        # `compose.logs` already returns redacted text (§9).
        output = compose.logs(compose_path, name).strip()
        if not output:
            continue
        printed_any = True
        console.print(f"\n[axion.info]── {name} ──[/]")
        console.print(output)

    # Without this, a stopped stack printed exactly nothing and exited 0: the
    # user could not tell "there are no logs" from "the command did nothing".
    if not printed_any:
        console.print(
            f"[axion.warn]No service returned any logs[/] ({', '.join(targets)}). "
            "The stack is probably not up — check with `axion-wizard doctor`."
        )



def run_uninstall(state: GlobalState, purge: bool = False) -> None:
    """Bring the stack down. With `--purge` it also deletes the volumes, and
    for that it requires typing the project name as confirmation (§9)."""
    from axion_wizard.services import compose

    compose_path = compose_path_of(state)
    project_name = state.project_dir.resolve().name

    # The `--dry-run` bail-out comes *before* asking for confirmation:
    # `--dry-run` promises to touch nothing, so there is nothing to confirm.
    # Asking first meant a non-interactive `--dry-run --purge` sat waiting for
    # an answer that never came and exited 1 ("wrong confirmation") without
    # having tried to delete anything.
    if state.dry_run:
        announce_dry_run(f"would run `docker compose down{' --volumes' if purge else ''}`")
        return

    if purge:
        console.print(
            "[axion.error]--purge will delete the data volumes:[/] Mattermost's "
            "database, the message history, the downloaded Ollama models and the "
            "WireGuard keys. This cannot be undone."
        )
        if not state.yes:
            import questionary

            answer = questionary.text(
                f"Type the project name ({project_name}) to confirm:"
            ).ask()
            if answer != project_name:
                console.print("[axion.warn]Wrong confirmation; nothing was deleted.[/]")
                raise typer.Exit(code=1)

    result = compose.down(compose_path, volumes=purge)
    if not result.ok:
        console.print(f"[axion.error]`docker compose down` failed:[/] {result.stderr.strip()}")
        raise typer.Exit(code=1)

    if purge:
        console.print("[axion.ok]Stack uninstalled and volumes deleted.[/]")
    else:
        console.print("[axion.ok]Stack uninstalled.[/] The data volumes are kept.")
