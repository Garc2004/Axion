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
        target = service or "todos los servicios"
        announce_dry_run(f"ejecutaría `docker compose up -d --build` para {target}")
        return

    services = [service] if service else list(MANAGED_SERVICES)
    s06_deploy.deploy(compose_path, services=services)

    # §6.4 exige comprobar la tag *efectiva* del contenedor, no solo la que
    # quedó escrita en el compose: quien edite el archivo a mano puede acabar
    # corriendo otro major de wg-easy, que se configura de forma
    # incompatible y deja el panel inutilizable sin un solo error en los logs.
    if WIREGUARD_SERVICE in services:
        s06_deploy.verify_wg_easy_tag(compose_path)

    # `axion-wizard up mattermost` recrea el contenedor con otra IP y deja a
    # nginx apuntando a la vieja: 502 en todo el stack sin que ningún
    # healthcheck se entere.
    if s06_deploy.refresh_nginx(compose_path, services):
        console.print("[axion.ok]nginx reiniciado[/] para reencontrar a Mattermost.")

    console.print("[axion.ok]Stack levantado.[/]")

    for status in compose.ps(compose_path):
        health = f" ({status.health})" if status.health else ""
        console.print(f"  {status.service}: {status.state}{health}")



def run_compose_down(state: GlobalState) -> None:
    from axion_wizard.services import compose

    compose_path = compose_path_of(state)

    if state.dry_run:
        announce_dry_run("ejecutaría `docker compose down` (conservando volúmenes)")
        return

    result = compose.down(compose_path)
    if result.ok:
        console.print("[axion.ok]Stack detenido.[/] Los volúmenes de datos se conservan.")
    else:
        console.print(f"[axion.error]`docker compose down` falló:[/] {result.stderr.strip()}")
        raise typer.Exit(code=1)



def run_compose_logs(state: GlobalState, service: str | None = None) -> None:
    from axion_wizard.services import compose

    compose_path = compose_path_of(state)
    targets = [service] if service else list(MANAGED_SERVICES)

    printed_any = False
    for name in targets:
        # `compose.logs` ya devuelve el texto redactado (§9).
        output = compose.logs(compose_path, name).strip()
        if not output:
            continue
        printed_any = True
        console.print(f"\n[axion.info]── {name} ──[/]")
        console.print(output)

    # Sin esto, un stack parado imprimía exactamente nada y salía con 0: el
    # usuario no podía distinguir "no hay logs" de "el comando no hizo nada".
    if not printed_any:
        console.print(
            f"[axion.warn]Ningún servicio devolvió logs[/] ({', '.join(targets)}). "
            "Probablemente el stack no esté levantado — comprobar con `axion-wizard doctor`."
        )



def run_uninstall(state: GlobalState, purge: bool = False) -> None:
    """Baja el stack. Con `--purge` borra además los volúmenes, y para eso
    exige escribir el nombre del proyecto como confirmación (§9)."""
    from axion_wizard.services import compose

    compose_path = compose_path_of(state)
    project_name = state.project_dir.resolve().name

    # El corte por `--dry-run` va *antes* de pedir confirmación: `--dry-run`
    # promete no tocar nada, así que no hay nada que confirmar. Preguntando
    # primero, un `--dry-run --purge` no interactivo se quedaba esperando una
    # respuesta que nunca llegaba y terminaba en 1 ("Confirmación incorrecta")
    # sin haber intentado borrar nada.
    if state.dry_run:
        announce_dry_run(f"ejecutaría `docker compose down{' --volumes' if purge else ''}`")
        return

    if purge:
        console.print(
            "[axion.error]--purge borrará los volúmenes de datos:[/] base de datos de "
            "Mattermost, historial de mensajes, modelos de Ollama descargados y las "
            "claves de WireGuard. Esto no se puede deshacer."
        )
        if not state.yes:
            import questionary

            answer = questionary.text(
                f"Escribe el nombre del proyecto ({project_name}) para confirmar:"
            ).ask()
            if answer != project_name:
                console.print("[axion.warn]Confirmación incorrecta; no se borró nada.[/]")
                raise typer.Exit(code=1)

    result = compose.down(compose_path, volumes=purge)
    if not result.ok:
        console.print(f"[axion.error]`docker compose down` falló:[/] {result.stderr.strip()}")
        raise typer.Exit(code=1)

    if purge:
        console.print("[axion.ok]Stack desinstalado y volúmenes borrados.[/]")
    else:
        console.print("[axion.ok]Stack desinstalado.[/] Los volúmenes de datos se conservan.")
