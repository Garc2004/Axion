"""App Typer y registro de subcomandos."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import typer
from rich.panel import Panel
from rich.text import Text

from axion_wizard import __version__, privileges
from axion_wizard.errors import AxionError, PlatformError
from axion_wizard.render import ui
from axion_wizard.render.console import console, error_console, set_no_color, set_quiet
from axion_wizard.utils import winconsole

app = typer.Typer(
    name="axion-wizard",
    help="Instalador/orquestador del stack AXION (Mattermost + WireGuard + Ollama + FastAPI).",
    no_args_is_help=False,
    add_completion=True,
    pretty_exceptions_enable=False,
)


@dataclass
class GlobalState:
    """Opciones globales compartidas por todos los subcomandos."""

    verbose: bool = False
    quiet: bool = False
    no_color: bool = False
    dry_run: bool = False
    yes: bool = False
    no_elevate: bool = False
    project_dir: Path = field(default_factory=Path.cwd)
    #: `install --unattended`: prohíbe cualquier prompt. Los pasos lo
    #: consultan para no quedarse esperando una respuesta que nunca llega.
    unattended: bool = False
    #: `install --config`: archivo TOML con la configuración completa.
    config_path: Path | None = None


state = GlobalState()

#: Subcomandos que sí necesitan privilegios, como lista explícita.
#:
#: Es un allowlist y no un denylist a propósito: §9 pide elevar solo en los
#: puntos que lo requieren, así que lo seguro por defecto es *no* elevar y
#: nombrar las excepciones. Con la lista invertida, cada subcomando nuevo
#: heredaría la elevación sin querer — que es justo lo que pasó con
#: `gen-cert`, que solo escribe un par de archivos.
#:
#: `None` representa el flujo por defecto (`axion-wizard` sin subcomando),
#: que equivale a `install`.
#:
#: La lista es documentación y punto de anclaje para los tests; quien
#: realmente eleva es cada comando con `_dispatch(..., elevate=True)`.
ELEVATION_REQUIRED_COMMANDS: frozenset[str | None] = frozenset(
    {None, "install", "up", "down", "uninstall", "set-webhook-token", "set-bot-token"}
)


def _render_error(exc: AxionError) -> None:
    body = Text()
    body.append(f"{exc.what}\n\n", style="bold")
    body.append(f"{exc.why}\n\n")
    if exc.steps:
        body.append("Qué hacer:\n", style="axion.heading")
        for i, step in enumerate(exc.steps, start=1):
            body.append(f"  {i}. ", style="axion.accent")
            body.append(f"{step}\n")
    error_console.print(
        Panel(
            body,
            title=f"[axion.error]{ui.GLYPH_FAIL} {exc.title}[/]",
            title_align="left",
            border_style="axion.error",
            padding=(1, 2),
        )
    )


def _dispatch(fn: Callable[[], None], *, elevate: bool = False) -> None:
    """Ejecuta el cuerpo de un comando, convirtiendo `AxionError` en un panel
    Rich + salida no-cero. Se invoca desde cada comando (no solo desde
    `main()`) porque Click/Typer no encaminan las excepciones de los
    subcomandos a través del wrapper de `main()` cuando se invoca `app`
    directamente (p.ej. bajo `CliRunner` en los tests).

    `elevate=True` pide privilegios *justo antes* de ejecutar el cuerpo, y no
    desde el callback del grupo, que es donde estaba antes. Click ejecuta ese
    callback antes de procesar el `--help` del subcomando, así que
    `axion-wizard install --help` abría un diálogo de UAC —y, al esperar
    ahora al proceso elevado, se quedaba bloqueado— solo por leer la ayuda.
    Desde aquí ya no puede pasar: Click imprime la ayuda y sale sin llegar a
    invocar el cuerpo del comando.
    """
    try:
        if elevate:
            ensure_elevated()
        fn()
    except AxionError as exc:
        _render_error(exc)
        if state.verbose:
            error_console.print_exception()
        raise typer.Exit(code=1) from None


def ensure_elevated() -> None:
    """Se asegura de correr con privilegios de administrador, relanzando el
    proceso si hace falta.

    Solo la invocan los comandos de `ELEVATION_REQUIRED_COMMANDS`, vía
    `_dispatch(..., elevate=True)`; nunca con `--no-elevate` y nunca en
    `--dry-run`, que por definición no toca el sistema. Tampoco eleva en
    silencio: primero dice para qué lo necesita.

    El `--project-dir` se le pasa al hijo de forma explícita porque en
    Windows no hereda el directorio de trabajo del padre (ver
    `privileges.relaunch_elevated_windows`), y sin él caería en su valor por
    defecto, `Path.cwd()`, que para un proceso lanzado por UAC es
    `C:\\Windows\\System32`.
    """
    if state.no_elevate or state.dry_run:
        return
    if privileges.is_elevated():
        return

    console.print(f"[axion.warn]{privileges.explain_elevation_reason()}[/]")
    console.print(
        "[axion.dim]Relanzando con privilegios elevados… "
        "(usa --no-elevate para continuar sin ellos)[/]"
    )

    project_dir = str(state.project_dir)
    try:
        exit_code = privileges.relaunch_elevated(
            leading_args=["--project-dir", project_dir],
            working_dir=project_dir,
        )
    except privileges.ElevationError as exc:
        raise PlatformError(
            what="No se pudieron obtener privilegios de administrador",
            why=str(exc),
            steps=[
                "Aceptar el diálogo de UAC (Windows) o introducir la contraseña de sudo (Linux).",
                "Abrir la terminal como Administrador/root y reintentar.",
                "O continuar sin elevar con --no-elevate (algunos pasos pueden fallar).",
            ],
        ) from exc

    # El hijo elevado corre en su propia ventana y ya pausa él antes de
    # cerrarla; que el padre pausara también obligaría a pulsar Enter dos
    # veces, en dos ventanas distintas, por una sola ejecución.
    winconsole.disable_pause()
    raise typer.Exit(code=exit_code)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"axion-wizard {__version__}")
        raise typer.Exit()


#: Subcarpeta donde se despliega por defecto cuando no se pasa
#: `--project-dir` ni ya hay un despliegue en el directorio actual.
DEFAULT_PROJECT_SUBDIR = "axion"


def _default_project_dir() -> Path:
    """Directorio de proyecto cuando no se pasó `--project-dir` (§4.2).

    Sin esto, ejecutar el binario tal cual se descargó —doble clic desde
    `~/Descargas`, por ejemplo— escribía `docker-compose.yml`, `.env`,
    `nginx/`, `backups/`… sueltos ahí mismo, mezclados con cualquier otra
    cosa que hubiera. Incidente real, no hipotético.

    Si ya hay un despliegue en el directorio actual (`docker-compose.yml`
    presente — el caso de quien siguió la recomendación del README de crear
    una carpeta dedicada y poner el binario dentro) se usa tal cual, sin
    anidar una carpeta más: ya está "dentro de una carpeta".
    """
    cwd = Path.cwd()
    if (cwd / "docker-compose.yml").exists():
        return cwd
    return cwd / DEFAULT_PROJECT_SUBDIR


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    version: bool | None = typer.Option(
        None,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Muestra la versión y termina.",
    ),
    verbose: bool = typer.Option(False, "--verbose", help="Salida detallada, incluye tracebacks."),
    quiet: bool = typer.Option(False, "--quiet", help="Suprime salida no esencial."),
    no_color: bool = typer.Option(False, "--no-color", help="Desactiva el color en la salida."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Imprime cada comando y escritura de archivo sin ejecutarlos."
    ),
    yes: bool = typer.Option(False, "--yes", help="Asume 'sí' en toda confirmación."),
    no_elevate: bool = typer.Option(
        False,
        "--no-elevate",
        help="No pedir privilegios de administrador (algunos pasos pueden fallar).",
    ),
    project_dir: Path | None = typer.Option(
        None, "--project-dir", help="Directorio del proyecto AXION.", file_okay=False
    ),
) -> None:
    state.verbose = verbose
    state.quiet = quiet
    state.no_color = no_color
    state.dry_run = dry_run
    state.yes = yes
    state.no_elevate = no_elevate
    # Absoluto desde el principio: el relanzamiento elevado arranca en otro
    # directorio de trabajo (§ `ensure_elevated`), así que una ruta relativa
    # como `--project-dir ./axion` apuntaría a otro sitio en el hijo.
    used_default_subdir = project_dir is None and not (Path.cwd() / "docker-compose.yml").exists()
    resolved_project_dir = project_dir if project_dir is not None else _default_project_dir()
    state.project_dir = resolved_project_dir.resolve()

    set_quiet(quiet)
    set_no_color(no_color)

    if used_default_subdir and not quiet:
        console.print(
            f"[axion.dim]Sin --project-dir: se usará {state.project_dir} "
            "(pásalo explícitamente para elegir otra carpeta).[/]"
        )

    if ctx.invoked_subcommand is None:
        from axion_wizard.commands import run_install

        _dispatch(lambda: run_install(state), elevate=True)


@app.command()
def install(
    unattended: bool = typer.Option(
        False, "--unattended", help="Sin prompts interactivos; requiere --config."
    ),
    config: Path | None = typer.Option(
        None, "--config", help="Archivo axion.toml con la configuración completa."
    ),
    tui: bool = typer.Option(
        False, "--tui", help="Interfaz a pantalla completa en vez de prompts secuenciales."
    ),
    restart: bool = typer.Option(
        False,
        "--restart",
        help="Ignora el progreso guardado y rehace la instalación desde el paso 1.",
    ),
) -> None:
    """Ejecuta el flujo completo de instalación."""
    from axion_wizard.commands import run_install

    _dispatch(
        lambda: run_install(
            state, unattended=unattended, config_path=config, tui=tui, restart=restart
        ),
        elevate=True,
    )


@app.command()
def reset(
    yes: bool = typer.Option(False, "--yes", "-y", help="No pedir confirmación."),
) -> None:
    """Olvida el progreso y hace que `install` empiece por el paso 1.

    No borra contenedores, volúmenes, `.env` ni el certificado — solo el
    registro de qué pasos se habían completado.
    """
    from axion_wizard.commands import run_reset

    _dispatch(lambda: run_reset(state, yes=yes))


@app.command()
def doctor() -> None:
    """Re-valida todo el stack desplegado sin modificarlo."""
    from axion_wizard.commands import run_doctor

    _dispatch(lambda: run_doctor(state))


@app.command(name="network-check")
def network_check() -> None:
    """Ejecuta solo las verificaciones de red (§4.2)."""
    from axion_wizard.commands import run_network_check

    _dispatch(lambda: run_network_check(state))


@app.command(name="gen-cert")
def gen_cert(
    host: str = typer.Argument(..., help="IP o dominio para el subjectAltName del certificado."),
) -> None:
    """Genera solo el certificado TLS."""
    from axion_wizard.commands import run_gen_cert

    _dispatch(lambda: run_gen_cert(state, host=host))


@app.command(name="set-webhook-token")
def set_webhook_token(
    token: str = typer.Argument(
        ..., help="Token del webhook saliente, generado por Mattermost al crearlo."
    ),
) -> None:
    """Guarda el token del webhook de Mattermost y recrea fastapi para usarlo."""
    from axion_wizard.commands import run_set_webhook_token

    _dispatch(lambda: run_set_webhook_token(state, token=token), elevate=True)


@app.command(name="set-bot-token")
def set_bot_token(
    token: str = typer.Argument(..., help="Token de un bot de Mattermost."),
) -> None:
    """Permite que la IA tarde lo que necesite: responde en el canal al terminar.

    Sin esto, Mattermost abandona la petición del webhook a los ~30 segundos
    y la respuesta de un modelo lento se pierde entera, sin error visible.
    """
    from axion_wizard.commands import run_set_bot_token

    _dispatch(lambda: run_set_bot_token(state, token=token), elevate=True)


models_app = typer.Typer(help="Catálogo de modelos de Ollama.")
app.add_typer(models_app, name="models")


@models_app.callback(invoke_without_command=True)
def models_list(ctx: typer.Context) -> None:
    """Lista modelos de Ollama compatibles con el hardware detectado."""
    if ctx.invoked_subcommand is None:
        from axion_wizard.commands import run_models_list

        _dispatch(lambda: run_models_list(state))


@models_app.command("pull")
def models_pull(name: str = typer.Argument(..., help="Nombre del modelo a descargar.")) -> None:
    """Descarga un modelo de Ollama."""
    from axion_wizard.commands import run_models_pull

    _dispatch(lambda: run_models_pull(state, name=name))


model_app = typer.Typer(
    help=(
        "Edita la IA: qué modelo usa y con qué instrucciones. Escribe .env y "
        "recrea el contenedor fastapi por ti."
    )
)
app.add_typer(model_app, name="model")


@model_app.callback(invoke_without_command=True)
def model_show(ctx: typer.Context) -> None:
    """Muestra el modelo y las instrucciones que la IA usa ahora mismo."""
    if ctx.invoked_subcommand is None:
        from axion_wizard.commands import run_model_show

        _dispatch(lambda: run_model_show(state))


@model_app.command("set")
def model_set(
    name: str = typer.Argument(..., help="Modelo de Ollama que debe usar la IA."),
    no_pull: bool = typer.Option(
        False, "--no-pull", help="No descargarlo si falta (fallará hasta que esté)."
    ),
) -> None:
    """Cambia el modelo de la IA: lo descarga, lo escribe en .env y recrea fastapi."""
    from axion_wizard.commands import run_model_set

    _dispatch(lambda: run_model_set(state, name=name, skip_pull=no_pull))


@model_app.command("choose")
def model_choose() -> None:
    """Elige el modelo de una lista ordenada según el hardware detectado."""
    from axion_wizard.commands import run_model_choose

    _dispatch(lambda: run_model_choose(state))


@model_app.command("prompt")
def model_prompt(
    text: str = typer.Argument(
        ...,
        help=(
            "Instrucciones permanentes de la IA (tono, idioma, qué es). "
            'Cadena vacía ("") para borrarlas.'
        ),
    ),
) -> None:
    """Edita las instrucciones permanentes de la IA."""
    from axion_wizard.commands import run_model_prompt

    _dispatch(lambda: run_model_prompt(state, prompt=text))


wireguard_app = typer.Typer(help="Gestión del panel WireGuard (wg-easy).")
app.add_typer(wireguard_app, name="wireguard")


@wireguard_app.command("add-client")
def wireguard_add_client(
    name: str = typer.Argument(..., help="Nombre del cliente a crear."),
) -> None:
    """Crea un cliente WireGuard y muestra su QR en terminal."""
    from axion_wizard.commands import run_wireguard_add_client

    _dispatch(lambda: run_wireguard_add_client(state, name=name))


@app.command()
def up(
    service: str | None = typer.Argument(None, help="Servicio a levantar (todos si se omite)."),
) -> None:
    """Atajo de `docker compose up -d`."""
    from axion_wizard.commands import run_compose_up

    _dispatch(lambda: run_compose_up(state, service=service), elevate=True)


@app.command()
def down() -> None:
    """Atajo de `docker compose down`."""
    from axion_wizard.commands import run_compose_down

    _dispatch(lambda: run_compose_down(state), elevate=True)


@app.command()
def logs(
    service: str | None = typer.Argument(None, help="Servicio del cual mostrar logs."),
) -> None:
    """Muestra las últimas líneas del log de cada servicio (no sigue el log)."""
    from axion_wizard.commands import run_compose_logs

    _dispatch(lambda: run_compose_logs(state, service=service))


@app.command()
def uninstall(
    purge: bool = typer.Option(False, "--purge", help="Borra también los volúmenes de datos."),
) -> None:
    """Baja el stack AXION."""
    from axion_wizard.commands import run_uninstall

    _dispatch(lambda: run_uninstall(state, purge=purge), elevate=True)


def main() -> None:
    """Punto de entrada del ejecutable.

    El `finally` no es decorativo: cuando el wizard se abre con doble clic
    desde el Explorador —o cuando es el hijo que UAC acaba de relanzar— la
    consola se creó para este proceso y Windows la destruye al terminar. Sin
    la pausa, cualquier salida (incluido el panel de error que se acaba de
    imprimir) desaparece antes de poder leerla y el arranque parece un
    cierre inesperado. `winconsole` decide solo si procede pausar; en una
    terminal normal, en una tubería o en CI no hace nada.
    """
    try:
        try:
            app()
        except AxionError as exc:
            _render_error(exc)
            if state.verbose:
                error_console.print_exception()
            raise SystemExit(1) from None
        except Exception as exc:  # noqa: BLE001 - último recurso, nunca traceback crudo sin --verbose
            error_console.print(f"[axion.error]Error inesperado:[/] {exc}")
            if state.verbose:
                error_console.print_exception()
            raise SystemExit(1) from None
    finally:
        winconsole.pause_if_console_would_close()


if __name__ == "__main__":
    main()
