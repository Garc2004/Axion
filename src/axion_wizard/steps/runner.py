"""Orquestación de los subcomandos de la CLI.

Cada función aquí es el punto de entrada que `cli.py` invoca para un
subcomando: hace de pegamento entre las opciones de la CLI y los servicios
(`services/`) y pasos (`steps/`), sin lógica de negocio propia.

`install` sigue pendiente (es el flujo interactivo de §4.3 completo); el
resto de subcomandos ya operan sobre los servicios reales.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from axion_wizard import ui
from axion_wizard.console import console
from axion_wizard.errors import ConfigError

if TYPE_CHECKING:
    from axion_wizard.cli import GlobalState

CERT_RELATIVE_DIR = Path("nginx") / "certs"
COMPOSE_FILENAME = "docker-compose.yml"


def _compose_path(state: GlobalState) -> Path:
    """Ruta del compose del proyecto, verificando que exista antes de que
    Docker falle con un mensaje mucho menos claro."""
    path = state.project_dir / COMPOSE_FILENAME
    if not path.exists():
        raise ConfigError(
            what=f"No se encontró {path}",
            why="Este subcomando opera sobre un stack ya generado por `axion-wizard install`.",
            steps=[
                "Ejecutar `axion-wizard install` primero.",
                "O pasar el directorio correcto con --project-dir.",
            ],
        )
    return path


def _announce_dry_run(action: str) -> None:
    console.print(f"[axion.info][dry-run][/] {action}")


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
        _announce_dry_run(f"borraría {path}")
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


def run_doctor(state: GlobalState) -> None:
    """Re-valida un stack ya desplegado sin modificarlo (§4.9). Reconstruye
    host/modelo/variante leyendo los artefactos en `--project-dir`, no
    depende de una corrida previa de `install` en esta misma sesión."""
    from axion_wizard.steps.s09_verify import (
        all_checks_passed,
        discover_deployment,
        render_checks_table,
        run_all_checks,
    )

    facts = discover_deployment(state.project_dir)
    results = asyncio.run(run_all_checks(facts))
    console.print(render_checks_table(results))
    if not all_checks_passed(results):
        raise typer.Exit(code=1)


def run_network_check(state: GlobalState) -> None:
    """Solo las verificaciones de red de §4.2."""
    from axion_wizard.detect import network as net

    table = ui.make_table("Verificaciones de red")
    table.add_column("Comprobación", style="axion.label")
    table.add_column("Resultado")
    table.add_column("Detalle", overflow="fold")

    iface = net.get_primary_interface()
    if iface is not None:
        table.add_row("Interfaz principal", ui.ok(), f"{iface.name} — {iface.ip}")
    else:
        table.add_row("Interfaz principal", ui.fail(), "sin interfaz con IP de LAN")

    for port_status in net.check_ports_psutil():
        label = f"Puerto {port_status.port}/{port_status.protocol}"
        if port_status.free:
            table.add_row(label, ui.ok("LIBRE"), "")
        else:
            table.add_row(label, ui.warn("OCUPADO"), port_status.used_by or "")

    public_ip = asyncio.run(net.get_public_ipv4())
    table.add_row(
        "IP pública (IPv4)",
        ui.status(bool(public_ip), fail_label="DESCONOCIDA"),
        public_ip or "no se pudo determinar",
    )

    for target, reachable in asyncio.run(net.check_connectivity()).items():
        table.add_row(
            f"Conectividad {target}",
            ui.status(reachable),
            "" if reachable else "inalcanzable — el pull de imágenes/modelos fallará",
        )

    console.print(table)
    console.print(
        "[axion.dim]CGNAT: comparar la IP pública de arriba con la WAN del router "
        "(§4.2). Si no coinciden, el port forwarding no funcionará.[/]"
    )


def run_gen_cert(state: GlobalState, host: str) -> None:
    """Genera el certificado TLS y verifica su SAN leyéndolo de vuelta (§4.4)."""
    from axion_wizard.services import certs

    cert_dir = state.project_dir / CERT_RELATIVE_DIR
    cert_path = cert_dir / "cert.crt"
    key_path = cert_dir / "cert.key"

    if state.dry_run:
        _announce_dry_run(f"generaría {cert_path} y {key_path} con SAN para {host!r}")
        return

    result = certs.generate_certificate(host, cert_path, key_path)
    # Releer del propio archivo, no confiar en lo que acabamos de construir.
    san_entries = certs.verify_certificate_has_san(result.cert_path)

    console.print(f"[axion.ok]Certificado generado:[/] {result.cert_path}")
    console.print(f"[axion.ok]Clave privada:[/] {result.key_path} (permisos restringidos)")
    console.print(f"[axion.info]SAN verificado:[/] {', '.join(san_entries)}")

    _reload_nginx_certs(state)


def _reload_nginx_certs(state: GlobalState) -> None:
    """Hace que nginx relea el certificado recién generado.

    `nginx/certs` entra por bind mount, así que el archivo nuevo ya está
    dentro del contenedor — pero nginx cargó el anterior en memoria al
    arrancar y lo seguirá sirviendo indefinidamente. Sin esto, `gen-cert`
    terminaba en verde y el navegador seguía viendo el certificado viejo, sin
    nada que explicara por qué.
    """
    from axion_wizard.services import compose
    from axion_wizard.steps import s06_deploy

    # La ruta se arma a mano en vez de con `_compose_path`: generar el
    # certificado antes de que exista un stack es legítimo —se usará al
    # desplegar—, así que aquí la ausencia del compose no es un error.
    compose_path = state.project_dir / COMPOSE_FILENAME
    if not compose_path.exists():
        return

    status = compose.get_service_status(compose_path, s06_deploy.NGINX_SERVICE)
    if status is None or not status.is_running:
        return

    if compose.restart(compose_path, s06_deploy.NGINX_SERVICE).ok:
        console.print("[axion.ok]nginx reiniciado:[/] ya sirve el certificado nuevo.")
    else:
        console.print(
            "[axion.warn]No se pudo reiniciar nginx[/], que seguirá sirviendo el "
            "certificado anterior. Aplícalo con: axion-wizard up nginx"
        )


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
        _announce_dry_run(f"escribiría MM_WEBHOOK_TOKEN en {env_path} y recrearía fastapi")
        return

    _compose_path(state)
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
        _announce_dry_run(f"escribiría MM_BOT_TOKEN en {env_path} y recrearía fastapi")
        return

    _compose_path(state)
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
        _announce_dry_run(f"descargaría el modelo {name!r} vía la API de Ollama")
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
    from axion_wizard.steps.s07_model import FASTAPI_SERVICE

    compose_path = _compose_path(state)
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
    _compose_path(state)  # falla con un mensaje claro si no hay stack
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
        _announce_dry_run(
            f"descargaría {name!r} si hiciera falta, escribiría OLLAMA_MODEL en .env "
            "y recrearía fastapi"
        )
        return

    compose_path = _compose_path(state)
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

    _compose_path(state)
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
        _announce_dry_run(f"{action} en .env y recrearía fastapi")
        return

    _compose_path(state)
    _apply_env_and_recreate_fastapi(state, {"OLLAMA_SYSTEM_PROMPT": prompt})
    if prompt:
        console.print(f"[axion.dim]Instrucciones activas: {prompt}[/]")
    else:
        console.print(
            "[axion.dim]Instrucciones borradas: la IA vuelve a responder según su "
            "propio entrenamiento.[/]"
        )


def run_wireguard_add_client(state: GlobalState, name: str) -> None:
    """Crea un cliente en el panel wg-easy y muestra su QR en la terminal (§4.8)."""
    import questionary

    from axion_wizard.services import wireguard as wg
    from axion_wizard.steps.s09_verify import discover_deployment

    facts = discover_deployment(state.project_dir)
    panel_url = wg.build_panel_url(facts.host)

    if state.dry_run:
        _announce_dry_run(f"crearía el cliente {name!r} en {panel_url}")
        return

    console.print(f"[axion.info]Panel WireGuard:[/] {panel_url}")
    console.print(f"[axion.warn]{wg.PANEL_HTTPS_WARNING}[/]")

    password = questionary.password("Contraseña del panel WireGuard:").ask()
    if not password:
        raise ConfigError(
            what="No se introdujo la contraseña del panel",
            why="Sin autenticarse contra wg-easy no se puede crear el cliente.",
            steps=["Reintentar introduciendo la contraseña configurada en el paso 3."],
        )

    async def _create() -> wg.WireguardClient:
        await wg.wait_for_panel_ready(panel_url)
        async with wg.WireguardPanelClient(panel_url) as panel:
            await panel.login(password)
            return await wg.create_client_with_qr(panel, name)

    client = asyncio.run(_create())

    console.print(f"\n[axion.ok]Cliente creado:[/] {client.name} (id {client.id})\n")
    console.print(wg.render_qr_terminal(client.config_text))
    console.print(
        "[axion.dim]Escanea el QR con la app de WireGuard, o importa la "
        "configuración manualmente desde el panel.[/]"
    )


def run_compose_up(state: GlobalState, service: str | None = None) -> None:
    from axion_wizard.services import compose
    from axion_wizard.steps import s06_deploy
    from axion_wizard.steps.s05_compose import MANAGED_SERVICES

    compose_path = _compose_path(state)

    if state.dry_run:
        target = service or "todos los servicios"
        _announce_dry_run(f"ejecutaría `docker compose up -d --build` para {target}")
        return

    services = [service] if service else list(MANAGED_SERVICES)
    s06_deploy.deploy(compose_path, services=services)

    # §6.4 exige comprobar la tag *efectiva* del contenedor, no solo la que
    # quedó escrita en el compose: quien edite el archivo a mano puede acabar
    # corriendo wg-easy v15, que ignora WG_HOST/PASSWORD_HASH en silencio y
    # deja el panel inutilizable sin un solo error en los logs.
    if s06_deploy.WIREGUARD_SERVICE in services:
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

    compose_path = _compose_path(state)

    if state.dry_run:
        _announce_dry_run("ejecutaría `docker compose down` (conservando volúmenes)")
        return

    result = compose.down(compose_path)
    if result.ok:
        console.print("[axion.ok]Stack detenido.[/] Los volúmenes de datos se conservan.")
    else:
        console.print(f"[axion.error]`docker compose down` falló:[/] {result.stderr.strip()}")
        raise typer.Exit(code=1)


def run_compose_logs(state: GlobalState, service: str | None = None) -> None:
    from axion_wizard.services import compose
    from axion_wizard.steps.s05_compose import MANAGED_SERVICES

    compose_path = _compose_path(state)
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

    compose_path = _compose_path(state)
    project_name = state.project_dir.resolve().name

    # El corte por `--dry-run` va *antes* de pedir confirmación: `--dry-run`
    # promete no tocar nada, así que no hay nada que confirmar. Preguntando
    # primero, un `--dry-run --purge` no interactivo se quedaba esperando una
    # respuesta que nunca llegaba y terminaba en 1 ("Confirmación incorrecta")
    # sin haber intentado borrar nada.
    if state.dry_run:
        _announce_dry_run(f"ejecutaría `docker compose down{' --volumes' if purge else ''}`")
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
