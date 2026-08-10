"""App Textual del instalador (`axion-wizard install --tui`).

Dos pantallas y un hilo:

1. **Formulario.** Recoge lo mismo que el paso 3 interactivo, con la misma
   validación en vivo (`utils.secrets`), y construye el `AxionConfig`.
2. **Progreso.** Lista los diez pasos con su estado y vuelca el log a un
   panel desplazable.

Los pasos corren en un worker en hilo aparte, no en el bucle de eventos: son
llamadas bloqueantes (subprocess, HTTP síncrono, `asyncio.run` interno) y
ejecutarlas en el hilo de la UI la dejaría congelada durante minutos, justo
mientras más información necesita mostrar.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import SecretStr
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Select,
    Static,
)

from axion_wizard.domain.config import AccessMode, AxionConfig, WireguardVariant
from axion_wizard.errors import AxionError
from axion_wizard.render import ui
from axion_wizard.steps.context import InstallContext
from axion_wizard.steps.s03_config import existing_postgres_password
from axion_wizard.utils import secrets as secret_utils

if TYPE_CHECKING:
    from axion_wizard.cli import GlobalState

#: Tema base de Textual: una paleta ya diseñada (azules/cian fríos,
#: contraste cuidado) en vez de inventar colores hex a mano. El resto de la
#: CSS de esta app se apoya en sus variables (`$primary`, `$success`,
#: `$warning`, `$error`, `$surface`…) para heredar esa cohesión sin
#: reimplementarla.
APP_THEME = "nord"

#: Estados de un paso, con su marca. Se mantienen aparte del widget para que
#: la pantalla de progreso no tenga que conocer detalles de presentación.
#: Los glifos vienen de `axion_wizard.render.ui` — los mismos que usan las tablas
#: de la CLI (`doctor`, `network-check`…) — para que un ✓ signifique lo
#: mismo aquí que allá. El color sí es propio: el markup de Textual no
#: resuelve variables de tema (`$success`) dentro de texto enriquecido,
#: solo en CSS, así que aquí se usan nombres de color Rich literales.
PENDING, RUNNING, DONE, FAILED, SKIPPED = "pending", "running", "done", "failed", "skipped"

_STATUS_MARKS = {
    PENDING: (ui.GLYPH_PENDING, "dim"),
    RUNNING: (ui.GLYPH_RUNNING, "bold cyan"),
    DONE: (ui.GLYPH_OK, "bold green"),
    FAILED: (ui.GLYPH_FAIL, "bold red"),
    SKIPPED: (ui.GLYPH_SKIPPED, "dim yellow"),
}


class StepLine(Static):
    """Una línea de la lista de pasos, con su marca de estado."""

    def __init__(self, index: int, total: int, title: str) -> None:
        super().__init__()
        self._index = index
        self._total = total
        self._title = title
        self._status = PENDING
        self._detail = ""

    def on_mount(self) -> None:
        self._refresh()

    def set_status(self, status: str, detail: str = "") -> None:
        self._status = status
        self._detail = detail
        self._refresh()

    @property
    def text(self) -> str:
        """El texto con marcado, tal como se pinta.

        Existe para que los tests puedan comprobar qué muestra la línea sin
        depender de cómo Textual guarde el contenido de un `Static`, que ha
        cambiado entre versiones.
        """
        mark, style = _STATUS_MARKS[self._status]
        detail = f"  [dim]{self._detail}[/]" if self._detail else ""
        return f"[{style}]{mark}[/] [{self._index}/{self._total}] {self._title}{detail}"

    def _refresh(self) -> None:
        self.update(self.text)


class ConfigScreen(Screen):
    """Paso 3 en formulario: mismo contenido, misma validación."""

    BINDINGS = [("escape", "app.quit", "Cancelar")]

    def __init__(self, state: GlobalState) -> None:
        super().__init__()
        self._state = state
        #: Último error de validación mostrado. Es lo que comprueban los
        #: tests: el mensaje es comportamiento, cómo lo pinte el `Static` no.
        self.last_error = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with VerticalScroll(id="form"):
            yield Static(self._environment_line(), id="environment-summary")

            yield Static("1 · Acceso", classes="section-title")
            with Vertical(classes="section"):
                yield Label("Modo de acceso")
                yield Select(
                    [
                        ("IP de la LAN (certificado autofirmado)", AccessMode.LAN.value),
                        ("Dominio propio (Let's Encrypt DNS-01)", AccessMode.DOMAIN.value),
                    ],
                    value=AccessMode.LAN.value,
                    id="access_mode",
                    allow_blank=False,
                )
                yield Label("IP o dominio de acceso")
                yield Input(placeholder="192.168.1.50", id="host")

            yield Static("2 · Seguridad", classes="section-title")
            with Vertical(classes="section"):
                yield Label("Contraseña del panel WireGuard")
                yield Static(
                    "No puede contener "
                    + ", ".join(f"`{c}`" for c in secret_utils.FORBIDDEN_PASSWORD_CHAR_REASONS)
                    + ": rompen el shell y los archivos .env.",
                    classes="hint",
                )
                yield Input(password=True, id="panel_password")
                yield Label("Repite la contraseña")
                yield Input(password=True, id="panel_password_repeat")

            yield Static("3 · Modelo", classes="section-title")
            with Vertical(classes="section"):
                yield Label("Modelo de IA (nombre en Ollama)")
                yield Input(placeholder="qwen2.5:1.5b", id="model")

            yield Static("", id="error", classes="error")
            with Horizontal(id="actions"):
                # Arranca deshabilitado: la variante de WireGuard la decide el
                # paso 1, que corre en un worker en hilo, y `on_mount` muestra
                # este formulario sin esperarlo. Quien rellenara y pulsara
                # antes de que terminara la detección se llevaba el valor por
                # defecto (`ports`) — en Linux nativo, la variante equivocada
                # y un compose que no corresponde a la plataforma.
                yield Button("Instalar", variant="primary", id="start", disabled=True)
                yield Button("Cancelar", variant="error", id="cancel")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "AXION Wizard"
        self.sub_title = f"Configuración — {self._state.project_dir}"
        self.query_one("#host", Input).focus()
        # La detección puede haber terminado ya (entorno cacheado, worker
        # rápido) antes de que esta pantalla se montara.
        if getattr(self.app, "environment_ready", False):
            self.mark_environment_ready()

    def mark_environment_ready(self) -> None:
        """Habilita el envío una vez el paso 1 ha decidido la variante."""
        self.query_one("#start", Button).disabled = False
        self.query_one("#environment-summary", Static).update(self._environment_line())

    def _environment_line(self) -> str:
        """Eco de lo que decidió el paso 1, para no dejar al usuario a
        ciegas sobre qué detectó el wizard antes de mostrar el formulario
        — la CLI lo muestra en una tabla completa; aquí, en una línea.

        Tres estados, y el primero es el que faltaba: mientras la detección
        corre no se puede afirmar nada, y enseñar el valor por defecto como
        si fuera un hallazgo es peor que decir que se está mirando.
        """
        context = getattr(self.app, "_install_context", None)
        facts = context.environment if context is not None else None
        if facts is not None:
            return (
                f"[dim]{facts.os_info.name} · Docker {facts.docker.docker_version or '?'} · "
                f"variante[/] [$primary]{facts.wireguard_variant}[/]"
            )
        if getattr(self.app, "environment_ready", False):
            return f"[dim]Variante WireGuard:[/] [$primary]{self.app.detected_variant}[/]"  # type: ignore[attr-defined]
        return "[dim]Detectando entorno…[/]"

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.app.exit()

    @on(Button.Pressed, "#start")
    def _start(self) -> None:
        try:
            config = self._build_config()
        except ValueError as exc:
            self.last_error = str(exc)
            self.query_one("#error", Static).update(f"{ui.GLYPH_FAIL} {exc}")
            self.query_one("#error", Static).add_class("visible")
            return
        self.last_error = ""
        self.query_one("#error", Static).remove_class("visible")
        self.app.begin_install(config)  # type: ignore[attr-defined]

    def _build_config(self) -> AxionConfig:
        host = self.query_one("#host", Input).value.strip()
        password = self.query_one("#panel_password", Input).value
        repeated = self.query_one("#panel_password_repeat", Input).value
        model = self.query_one("#model", Input).value.strip()

        if not host:
            raise ValueError("El host no puede estar vacío.")
        if not model:
            raise ValueError("Indica el modelo de Ollama a usar.")
        if password != repeated:
            raise ValueError("Las contraseñas no coinciden.")
        try:
            secret_utils.validate_wireguard_password(password)
        except secret_utils.WeakPasswordError as exc:
            raise ValueError(str(exc)) from exc
        if len(password) < 8:
            raise ValueError("La contraseña del panel debe tener al menos 8 caracteres.")

        access_mode = AccessMode(str(self.query_one("#access_mode", Select).value))
        variant = self.app.detected_variant  # type: ignore[attr-defined]

        try:
            return AxionConfig(
                access_mode=access_mode,
                host=host,
                wireguard_variant=WireguardVariant(variant),
                # Se reutiliza la contraseña ya escrita en `.env`, si la hay, en
                # vez de generar una nueva. Postgres solo aplica
                # `POSTGRES_PASSWORD` al inicializar su volumen y la ignora en
                # todo arranque posterior: una contraseña nueva sobre un volumen
                # ya inicializado deja a Mattermost sin poder autenticarse, sin
                # ningún error que lo explique. El camino de la CLI ya lo evitaba
                # (ver `s03_config.existing_postgres_password`, que documenta el
                # incidente); este no, y reintroducía el mismo fallo.
                postgres_password=SecretStr(
                    existing_postgres_password(self._state.project_dir)
                    or secret_utils.generate_hex_secret()
                ),
                wireguard_admin_password_hash=SecretStr(secret_utils.hash_password(password)),
                ollama_model=model,
                project_dir=self._state.project_dir,
            )
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


class ProgressScreen(Screen):
    """Los diez pasos con su estado, y el log debajo."""

    BINDINGS = [("q", "app.quit", "Salir")]

    def __init__(self, titles: list[str]) -> None:
        super().__init__()
        self._titles = titles
        self._lines: dict[int, StepLine] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        steps_container = Vertical(id="steps")
        steps_container.border_title = "Progreso"
        with steps_container:
            for index, title in enumerate(self._titles, start=1):
                line = StepLine(index, len(self._titles), title)
                self._lines[index - 1] = line
                yield line
        log = RichLog(id="log", highlight=True, markup=True, wrap=True)
        log.border_title = "Registro"
        yield log
        yield Footer()

    def on_mount(self) -> None:
        self.title = "AXION Wizard"
        self.sub_title = "Instalando…"

    def set_step(self, index: int, status: str, detail: str = "") -> None:
        line = self._lines.get(index)
        if line is not None:
            line.set_status(status, detail)

    def log_line(self, text: str) -> None:
        self.query_one("#log", RichLog).write(text)


class AxionInstallerApp(App):
    """App raíz: formulario → progreso, con los pasos en un worker.

    El aspecto se apoya en el tema `nord` de Textual (ver `APP_THEME`) más
    esta CSS, que solo añade estructura: agrupar el formulario en secciones
    con título de borde, dar peso visual a la sección activa
    (`:focus-within`), y tratar el error de validación como una alerta real
    —bordeada, con fondo— en vez de una línea de texto rojo suelta.
    """

    CSS = """
    Screen { background: $surface; }

    #form { padding: 1 2; }

    #environment-summary {
        color: $text-muted;
        margin-bottom: 1;
    }

    /* Encabezados de sección deliberadamente ligeros: una etiqueta con
    reborde inferior, no una caja completa. Un borde por sección (3 en este
    formulario) cuesta 2 filas de chrome cada uno más el padding — en una
    terminal de 24-30 filas eso empuja los botones fuera de la vista sin
    aportar mucho más que un texto en color ya no aporte. */
    .section-title {
        text-style: bold;
        color: $primary;
        border-bottom: solid $primary 40%;
        margin-top: 1;
        width: 100%;
    }
    .section-title:first-of-type { margin-top: 0; }

    .section { height: auto; }
    .section Label {
        text-style: bold;
        margin-top: 1;
    }
    .section Label:first-child {
        margin-top: 0;
    }

    .hint {
        color: $text-muted;
        text-style: italic;
        margin-bottom: 1;
    }

    .error {
        display: none;
        color: $error;
        text-style: bold;
    }
    .error.visible {
        display: block;
        border: round $error;
        background: $error 10%;
        padding: 0 1;
        margin-top: 1;
    }

    #actions {
        margin-top: 1;
        height: auto;
        align-horizontal: right;
    }
    #actions Button { margin-left: 2; }

    #steps {
        height: auto;
        max-height: 40%;
        padding: 1 2;
        border: round $primary 40%;
        border-title-color: $primary;
        border-title-style: bold;
        margin: 1 2 0 2;
    }

    #log {
        border: round $primary 40%;
        border-title-color: $primary;
        border-title-style: bold;
        margin: 1 2 1 2;
        padding: 0 1;
    }
    """

    def __init__(self, state: GlobalState) -> None:
        super().__init__()
        self._state = state
        self.succeeded = False
        self.detected_variant = WireguardVariant.PORTS.value
        #: `True` en cuanto el paso 1 ha decidido la variante. Hasta entonces
        #: el formulario no deja enviar: ver el botón `#start` de `ConfigScreen`.
        self.environment_ready = False
        self._install_context = InstallContext(project_dir=state.project_dir)
        self._install_steps: list = []
        self.theme = APP_THEME

    def on_mount(self) -> None:
        # El paso 1 no pregunta nada y decide la variante que el formulario
        # necesita, así que se lanza antes de mostrarlo. El formulario aparece
        # de inmediato (esperar en negro se ve como un cuelgue) pero con el
        # envío bloqueado hasta que la detección termina.
        self._detect_environment()
        self.push_screen(ConfigScreen(self._state))

    @work(thread=True)
    def _detect_environment(self) -> None:
        from axion_wizard.steps.s01_environment import EnvironmentStep

        quiet_state = _quiet_copy(self._state)
        try:
            EnvironmentStep(quiet_state, self._install_context).run()
        except AxionError as exc:
            self.call_from_thread(self._fail_early, exc)
            return
        facts = self._install_context.environment
        if facts is not None:
            self.detected_variant = facts.wireguard_variant
        self.environment_ready = True
        self.call_from_thread(self._announce_environment_ready)

    def _announce_environment_ready(self) -> None:
        screen = self.screen
        if isinstance(screen, ConfigScreen):
            screen.mark_environment_ready()

    def _fail_early(self, exc: AxionError) -> None:
        self.exit(message=f"{exc.title}: {exc.what}\n\n{exc.why}")

    def begin_install(self, config: AxionConfig) -> None:
        """Arranca los pasos 2-9 con la configuración ya resuelta."""
        from axion_wizard.steps import orchestrator

        self._install_context.config = config
        # Desatendido a propósito: Textual tiene la terminal, así que ningún
        # paso puede abrir un prompt de questionary por debajo.
        install_state = _quiet_copy(self._state, unattended=True)
        self._install_steps = orchestrator.build_steps(install_state, self._install_context)

        screen = ProgressScreen([step.title or step.name for step in self._install_steps])
        self.switch_screen(screen)
        self._run_steps(install_state)

    def _mark_resolved_steps_complete(self, install_state: GlobalState) -> None:
        """Da por hechos los pasos que la TUI ya resolvió por su cuenta.

        El entorno se detectó al arrancar y la configuración la puso el
        formulario, así que no hay que volver a ejecutarlos. Se marcan en el
        *estado persistido* en vez de saltárselos con un `if` dentro del
        bucle —que es lo que se hacía— para que el mecanismo de reanudación
        los vea igual que a cualquier otro paso: al reanudar, `run_steps`
        llama a su `restore()` y repuebla el contexto desde el disco.
        """
        from axion_wizard.utils import state as state_store

        if install_state.dry_run:
            return
        wizard_state = state_store.load_state(self._install_context.project_dir)
        for name, message in (
            ("environment", "resuelto por la TUI antes del formulario"),
            ("config", "resuelto en el formulario de la TUI"),
        ):
            wizard_state.mark_complete(name, message)
        state_store.save_state(self._install_context.project_dir, wizard_state)

    @work(thread=True)
    def _run_steps(self, install_state: GlobalState) -> None:
        """Ejecuta los pasos por el mismo camino que la CLI.

        Este worker tenía su propio bucle, copiado de `orchestrator.run_steps`
        pero sin la persistencia del progreso: una instalación `--tui`
        interrumpida no se reanudaba, pese a que el README lo promete para
        `install`. Ahora delega en el orquestador y solo aporta el reporter.
        """
        from axion_wizard.steps import orchestrator

        screen = self.screen
        assert isinstance(screen, ProgressScreen)

        self._mark_resolved_steps_complete(install_state)
        reporter = _TuiStepReporter(self, screen)

        try:
            all_ok = orchestrator.run_steps(
                install_state, self._install_context, self._install_steps, reporter=reporter
            )
        except AxionError as exc:
            all_ok = False
            reporter.report_error(exc)

        self.succeeded = all_ok
        self.call_from_thread(self._finish, all_ok)

    def _finish(self, all_ok: bool) -> None:
        screen = self.screen
        if not isinstance(screen, ProgressScreen):
            return

        screen.sub_title = "Completado" if all_ok else "Terminado con errores"
        config = self._install_context.config
        if config is not None:
            # Mismo contenido que el panel de cierre de la CLI
            # (`orchestrator.render_closing_summary`): dónde entrar y qué
            # quedó pendiente. Estaba duplicado a mano aquí, y solo se
            # mostraba cuando todo había ido bien — justo al revés de cuando
            # más falta hace saber por dónde entrar a lo que sí funciona.
            screen.log_line("")
            screen.log_line(f"[bold green]Mattermost:[/] https://{config.host}")
            screen.log_line(
                f"[bold green]Panel WireGuard:[/] http://{config.host}:51821 "
                "[dim](http, no https)[/]"
            )
            screen.log_line(f"[bold green]Modelo de IA:[/] {config.ollama_model}")
        for warning in self._install_context.warnings:
            screen.log_line(f"[yellow]Aviso:[/] {warning}")
        screen.log_line(
            "\n[dim]Re-validar en cualquier momento con: axion-wizard doctor[/]"
        )
        screen.log_line("[dim]Pulsa `q` para salir.[/]")


class _TuiStepReporter:
    """Traduce los eventos de `orchestrator.run_steps` a la pantalla Textual.

    Todo pasa por `call_from_thread` porque el orquestador corre en un worker
    en hilo aparte: tocar widgets desde ahí directamente es una condición de
    carrera con el bucle de eventos.

    Los índices del orquestador son 1-based (son para enseñar, `[3/9]`) y los
    de `ProgressScreen` 0-based (son posiciones de lista); la conversión vive
    aquí y en un solo sitio.
    """

    def __init__(self, app: AxionInstallerApp, screen: ProgressScreen) -> None:
        self._app = app
        self._screen = screen
        #: Último paso arrancado. Cuando un paso lanza `AxionError`, el
        #: orquestador re-lanza sin llegar a `on_step_finished`, así que sin
        #: recordarlo su línea se quedaría girando en "ejecutando" para
        #: siempre — con el error impreso justo debajo.
        self._current_index: int | None = None

    def _set(self, index: int, status: str, detail: str = "") -> None:
        self._app.call_from_thread(self._screen.set_step, index - 1, status, detail)

    def _log(self, text: str) -> None:
        self._app.call_from_thread(self._screen.log_line, text)

    def on_step_resumed(self, index: int, total: int, step: object) -> None:
        self._set(index, DONE, "ya completado, se reanuda")

    def on_step_invalidated(self, index: int, total: int, step: object, reason: str) -> None:
        self._set(index, PENDING, f"se rehace: {reason}")
        self._log(f"[yellow]Se rehace el paso {index}:[/] {reason}")

    def on_step_skipped(self, index: int, total: int, step: object, reason: str) -> None:
        self._set(index, SKIPPED, reason)

    def on_step_started(self, index: int, total: int, step: object) -> None:
        self._current_index = index
        self._set(index, RUNNING)

    def on_step_finished(self, index: int, total: int, step: object, result: object) -> None:
        self._current_index = None
        ok = bool(getattr(result, "ok", False))
        message = str(getattr(result, "message", ""))
        title = str(getattr(step, "title", "") or getattr(step, "name", ""))
        self._set(index, DONE if ok else FAILED, message)
        self._log(f"{title}: {message}")

    def on_note(self, message: str) -> None:
        self._log(f"[yellow]{message}[/]")

    def report_error(self, exc: AxionError) -> None:
        """El panel de error de la CLI, desarmado en líneas para el log."""
        if self._current_index is not None:
            self._set(self._current_index, FAILED, exc.what)
            self._current_index = None
        self._log(f"[bold red]{exc.what}[/]\n{exc.why}")
        for number, action in enumerate(exc.steps, start=1):
            self._log(f"  {number}. {action}")


def _quiet_copy(state: GlobalState, **overrides: object) -> GlobalState:
    """Copia de `GlobalState` con la salida a consola silenciada.

    Los pasos imprimen con Rich sobre stdout, y Textual es dueño de la
    pantalla: sin silenciarlos, sus tablas y paneles se cuelan por encima de
    la interfaz y la dejan ilegible.
    """
    from dataclasses import replace

    return replace(state, quiet=True, **overrides)  # type: ignore[arg-type]
