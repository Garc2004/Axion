"""`models`, `model` and the Mattermost token setters.

Everything that decides *what the AI is* and *how Mattermost reaches it*:
which model answers, with what standing instructions, and the two tokens
that switch the FastAPI bridge between its synchronous and asynchronous
modes.

Changing the model used to take three manual steps and knowing all three:
pull it, edit `OLLAMA_MODEL` in `.env` by hand, and recreate the fastapi
container with the right `docker compose` invocation (a `restart` will not
do: environment variables are fixed when the container is created).
Forgetting the third left the user staring at an AI still answering with
the old model, with no error anywhere. These commands do all three.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from axion_wizard.commands._common import announce_dry_run, compose_path_of
from axion_wizard.domain.stack import FASTAPI_SERVICE
from axion_wizard.errors import ConfigError
from axion_wizard.render import ui
from axion_wizard.render.console import console

if TYPE_CHECKING:
    from axion_wizard.cli import GlobalState



def _validated_token(token: str, *, label: str, source_hint: str) -> str:
    """Comprueba que un token se puede escribir en un `.env` sin romperlo."""
    from axion_wizard.utils import secrets as secret_utils

    token = token.strip()
    if not token:
        raise ConfigError(
            what=f"{label.capitalize()} no puede estar vacío",
            why="Sin un valor no hay nada que escribir en .env.",
            steps=[source_hint],
        )
    try:
        secret_utils.validate_env_value(token, label=label)
    except secret_utils.InvalidEnvValueError as exc:
        raise ConfigError(
            what="El token contiene un carácter no válido",
            why=str(exc),
            steps=["Revisar que se copió el token completo, sin espacios ni caracteres extra."],
        ) from exc
    return token



def run_set_webhook_token(state: GlobalState, token: str) -> None:
    """Guarda `MM_WEBHOOK_TOKEN` en `.env` y recrea fastapi para que lo tome
    (§4.5.1). Sin esto, activar la validación del token exigía editar el
    `.env` a mano y recordar los comandos exactos de `docker compose` para
    aplicarlo — justo lo que este comando evita.
    """
    from axion_wizard.utils import secrets as secret_utils

    token = _validated_token(
        token,
        label="el token",
        source_hint="Copiar el token desde Mattermost: Integraciones → Webhooks salientes.",
    )
    env_path = state.project_dir / ".env"

    if state.dry_run:
        announce_dry_run(f"escribiría MM_WEBHOOK_TOKEN en {env_path} y recrearía fastapi")
        return

    compose_path_of(state)
    secret_utils.register_secret(token)
    _apply_env_and_recreate_fastapi(state, {"MM_WEBHOOK_TOKEN": token})



def run_set_bot_token(state: GlobalState, token: str) -> None:
    """Guarda `MM_BOT_TOKEN` y pasa el puente al modo asíncrono.

    Es el interruptor entre los dos modos de `fastapi/main.py`: con un token
    de bot, el puente contesta al webhook al instante y publica la respuesta
    por la API cuando el modelo termina. Sin él, Mattermost abandona la
    petición a los ~30s y la respuesta de un modelo lento se pierde entera,
    sin que quede rastro del motivo en ningún log.
    """
    from axion_wizard.utils import secrets as secret_utils

    token = _validated_token(
        token,
        label="el token del bot",
        source_hint=(
            "Crear un bot en Mattermost (Integraciones → Cuentas de bot) y copiar su token."
        ),
    )
    env_path = state.project_dir / ".env"

    if state.dry_run:
        announce_dry_run(f"escribiría MM_BOT_TOKEN en {env_path} y recrearía fastapi")
        return

    compose_path_of(state)
    secret_utils.register_secret(token)
    _apply_env_and_recreate_fastapi(state, {"MM_BOT_TOKEN": token})
    console.print(
        "[axion.ok]Modo asíncrono activo:[/] la IA ya puede tardar lo que necesite; "
        "su respuesta se publicará en el canal en cuanto esté."
    )
    console.print(
        "[axion.dim]El bot tiene que estar añadido al equipo y a los canales donde "
        "deba responder, o Mattermost rechazará la publicación.[/]"
    )



def run_models_list(state: GlobalState) -> None:
    """Catálogo en tres niveles, ordenado por adecuación al hardware (§5)."""
    from axion_wizard.detect.hardware import detect_hardware
    from axion_wizard.services import ollama

    hardware = detect_hardware()
    catalog = asyncio.run(
        ollama.build_catalog(ram_gb=hardware.ram_total_gb, has_gpu=hardware.has_gpu)
    )
    recommended = ollama.recommended_model(catalog, hardware.ram_total_gb, hardware.has_gpu)

    gpu_label = ", ".join(g.name or g.vendor for g in hardware.gpus) or "sin GPU dedicada"
    console.print(
        f"[axion.info]Hardware detectado:[/] {hardware.ram_total_gb:.1f} GB RAM, "
        f"{hardware.cpu_logical} núcleos, {gpu_label}"
    )

    table = ui.make_table("Modelos de Ollama")
    table.add_column("")
    table.add_column("Modelo", style="axion.label")
    table.add_column("Tamaño")
    table.add_column("RAM mín.")
    table.add_column("Estado", overflow="fold")

    for model in catalog:
        marker = ""
        if recommended is not None and model.name == recommended.name:
            marker = f"[axion.ok]{ui.GLYPH_OK}[/]"

        reason = ollama.suitability_reason(model, hardware.ram_total_gb, hardware.has_gpu)
        if model.installed:
            model_status = ui.ok("ya instalado")
        elif reason:
            # Se muestran, no se ocultan: el usuario decide (§5).
            model_status = ui.warn(reason)
        else:
            model_status = "[axion.dim]compatible[/]"

        table.add_row(
            marker,
            model.name,
            f"{model.size_gb:.1f} GB" if model.size_bytes else "—",
            f"{model.min_ram_gb:g} GB" if ollama.has_known_requirements(model) else "?",
            model_status,
        )

    console.print(table)
    console.print(
        "[axion.dim]Descargar uno con: axion-wizard models pull <nombre>. "
        "La librería de Ollama crece constantemente: cualquier nombre válido sirve, "
        "aunque no aparezca en esta lista.[/]"
    )



def run_models_pull(state: GlobalState, name: str) -> None:
    """Descarga con barra de progreso real, parseando el stream de `/api/pull`."""
    from rich.progress import BarColumn, DownloadColumn, Progress, SpinnerColumn, TextColumn

    from axion_wizard.services import ollama

    if state.dry_run:
        announce_dry_run(f"descargaría el modelo {name!r} vía la API de Ollama")
        return

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        console=console,
    ) as progress:
        task_id = progress.add_task(f"Descargando {name}", total=None)

        def on_progress(update: ollama.PullProgress) -> None:
            if update.total > 0:
                progress.update(
                    task_id,
                    total=update.total,
                    completed=update.completed,
                    description=f"{name} — {update.status}",
                )
            else:
                progress.update(task_id, description=f"{name} — {update.status}")

        asyncio.run(ollama.pull_model(name, on_progress))

    console.print(f"[axion.ok]Modelo descargado:[/] {name}")
    console.print(
        f"[axion.dim]Para que la IA lo use: axion-wizard model set {name} "
        "(escribe .env y recrea fastapi por ti).[/]"
    )



# --- edición de la IA (§5) -------------------------------------------------------
#
# Cambiar el modelo exigía tres pasos manuales y saberse los tres: descargarlo,
# editar OLLAMA_MODEL en `.env` a mano y recrear el contenedor de fastapi con
# el `docker compose` correcto (un `restart` no vale: las variables de entorno
# se fijan al crear el contenedor). Olvidar el tercero deja al usuario mirando
# una IA que sigue respondiendo con el modelo viejo, sin ningún error.
#
# `axion-wizard model` hace los tres.


def _apply_env_and_recreate_fastapi(state: GlobalState, updates: dict[str, str]) -> None:
    """Escribe claves en `.env` y recrea fastapi para que las tome.

    Mismo camino que `run_set_webhook_token`: `update_env_value` preserva el
    resto del archivo, y `s06_deploy` levanta y espera a que el contenedor
    vuelva a estar sano en vez de darlo por bueno nada más lanzarlo.
    """
    from axion_wizard.steps import s06_deploy
    from axion_wizard.steps.s05_compose import update_env_value

    compose_path = compose_path_of(state)
    env_path = state.project_dir / ".env"

    for key, value in updates.items():
        update_env_value(env_path, key, value)
    console.print(f"[axion.ok]Guardado en {env_path}:[/] {', '.join(updates)}")

    console.print("[axion.dim]Recreando el contenedor fastapi para aplicarlo…[/]")
    s06_deploy.deploy(compose_path, services=[FASTAPI_SERVICE])
    s06_deploy.wait_for_healthy(compose_path, services=[FASTAPI_SERVICE])
    console.print("[axion.ok]Listo. La IA ya está usando la nueva configuración.[/]")



def _current_ai_settings(state: GlobalState) -> dict[str, str]:
    from axion_wizard.steps.s05_compose import existing_env_value

    return {
        "OLLAMA_MODEL": existing_env_value(state.project_dir, "OLLAMA_MODEL") or "(sin definir)",
        "OLLAMA_SYSTEM_PROMPT": existing_env_value(state.project_dir, "OLLAMA_SYSTEM_PROMPT")
        or "(sin instrucciones: el modelo responde según su entrenamiento)",
    }



def run_model_show(state: GlobalState) -> None:
    """Qué modelo y qué instrucciones está usando la IA ahora mismo."""
    compose_path_of(state)  # falla con un mensaje claro si no hay stack
    current = _current_ai_settings(state)

    table = ui.make_table("Configuración actual de la IA")
    table.add_column("Ajuste", style="axion.label")
    table.add_column("Valor", overflow="fold")
    table.add_row("Modelo", f"[axion.info]{current['OLLAMA_MODEL']}[/]")
    table.add_row("Instrucciones", current["OLLAMA_SYSTEM_PROMPT"])
    console.print(table)
    console.print(
        "[axion.dim]Cambiar el modelo: axion-wizard model set <nombre> "
        "(o `model choose` para elegirlo de una lista).\n"
        'Cambiar las instrucciones: axion-wizard model prompt "<texto>"[/]'
    )



def run_model_set(state: GlobalState, name: str, skip_pull: bool = False) -> None:
    """Cambia el modelo de la IA de punta a punta: descarga, `.env` y recreado."""
    from axion_wizard.services import ollama

    name = name.strip()
    if not name:
        raise ConfigError(
            what="No se indicó ningún modelo",
            why="Hace falta el nombre del modelo en Ollama (p.ej. `qwen2.5:1.5b`).",
            steps=[
                "Ver los compatibles con este hardware: axion-wizard models",
                "Elegirlo de una lista: axion-wizard model choose",
            ],
        )

    if state.dry_run:
        announce_dry_run(
            f"descargaría {name!r} si hiciera falta, escribiría OLLAMA_MODEL en .env "
            "y recrearía fastapi"
        )
        return

    compose_path = compose_path_of(state)
    already_installed = name in ollama.installed_model_names(
        asyncio.run(ollama.list_installed_models())
    )

    if already_installed:
        console.print(f"[axion.ok]El modelo {name} ya está descargado.[/]")
    elif skip_pull:
        console.print(
            f"[axion.warn]{name} no está descargado y se pidió no descargarlo:[/] "
            "la IA fallará hasta que lo esté."
        )
    else:
        # Sin esto, el fallo llega más tarde y en otro sitio: fastapi arranca
        # con un modelo que Ollama no tiene y cada mensaje devuelve un error
        # que no menciona la descarga por ninguna parte.
        run_models_pull(state, name=name)

    _apply_env_and_recreate_fastapi(state, {"OLLAMA_MODEL": name})
    console.print(f"[axion.dim]Comprobar el conjunto: axion-wizard doctor ({compose_path}).[/]")



def run_model_choose(state: GlobalState) -> None:
    """Elige el modelo de una lista ordenada por adecuación al hardware (§5).

    Es el camino para quien no se sabe los nombres de memoria: el mismo
    catálogo de tres niveles y el mismo orden que el paso 3 del instalador.
    """
    import questionary

    from axion_wizard.detect.hardware import detect_hardware
    from axion_wizard.services import ollama
    from axion_wizard.steps.prompts import require_interactive_input
    from axion_wizard.steps.s03_config import build_model_choices

    compose_path_of(state)
    require_interactive_input("Elegir el modelo de forma interactiva")

    current = _current_ai_settings(state)["OLLAMA_MODEL"]
    console.print(f"[axion.info]Modelo actual:[/] {current}")

    hardware = detect_hardware()
    catalog = asyncio.run(
        ollama.build_catalog(ram_gb=hardware.ram_total_gb, has_gpu=hardware.has_gpu)
    )
    recommended = ollama.recommended_model(catalog, hardware.ram_total_gb, hardware.has_gpu)
    choices, default = build_model_choices(catalog, recommended, hardware)

    answer = questionary.select(
        "¿Qué modelo debe usar la IA?", choices=choices, default=default
    ).ask()
    if answer is None:
        console.print("[axion.warn]Sin cambios.[/]")
        return
    if answer == ollama.OTHER_MODEL_SENTINEL:
        answer = (
            questionary.text("Nombre del modelo en Ollama:").ask() or ""
        ).strip()
        if not answer:
            console.print("[axion.warn]Sin cambios.[/]")
            return

    run_model_set(state, name=str(answer))



def run_model_prompt(state: GlobalState, prompt: str) -> None:
    """Edita las instrucciones permanentes de la IA (tono, idioma, papel).

    Se pasan a Ollama como `system` en cada petición, así que aplican a toda
    conversación sin que el usuario tenga que repetirlas cada vez.
    """
    from axion_wizard.utils import secrets as secret_utils

    prompt = prompt.strip()
    try:
        secret_utils.validate_env_value(prompt, label="las instrucciones")
    except secret_utils.InvalidEnvValueError as exc:
        raise ConfigError(
            what="Las instrucciones contienen un carácter que rompería el .env",
            why=str(exc),
            steps=["Reescribirlas sin `$`, comilla invertida ni `!`."],
        ) from exc

    if state.dry_run:
        action = "borraría las instrucciones de la IA" if not prompt else "escribiría las nuevas"
        announce_dry_run(f"{action} en .env y recrearía fastapi")
        return

    compose_path_of(state)
    _apply_env_and_recreate_fastapi(state, {"OLLAMA_SYSTEM_PROMPT": prompt})
    if prompt:
        console.print(f"[axion.dim]Instrucciones activas: {prompt}[/]")
    else:
        console.print(
            "[axion.dim]Instrucciones borradas: la IA vuelve a responder según su "
            "propio entrenamiento.[/]"
        )
