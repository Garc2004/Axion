"""The installer's Textual app (`axion-wizard install --tui`).

Two screens and one thread:

1. **Form.** Collects the same things the interactive step 3 does, with the
   same live validation (`utils.secrets`), and builds the `AxionConfig`.
2. **Progress.** Lists the ten steps with their status and dumps the log into
   a scrollable panel.

The steps run in a worker on a separate thread, not on the event loop: they
are blocking calls (subprocess, synchronous HTTP, an internal `asyncio.run`)
and running them on the UI thread would freeze it for minutes — precisely
when it has the most to show.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import SecretStr
from rich.console import RenderableType
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
from axion_wizard.steps.s03_config import DEFAULT_PANEL_USERNAME, existing_postgres_password
from axion_wizard.utils import secrets as secret_utils

if TYPE_CHECKING:
    from axion_wizard.cli import GlobalState

#: Textual's base theme: a palette that has already been designed (cool
#: blues/cyans, careful contrast) rather than inventing hex colours by hand.
#: The rest of this app's CSS leans on its variables (`$primary`, `$success`,
#: `$warning`, `$error`, `$surface`…) to inherit that cohesion without
#: reimplementing it.
APP_THEME = "nord"

#: A step's states, with their marks. Kept apart from the widget so the
#: progress screen does not have to know presentation details. The glyphs come
#: from `axion_wizard.render.ui` — the same ones the CLI's tables use
#: (`doctor`, `network-check`…) — so that a ✓ means the same thing here as
#: there. The colour is this module's own: Textual's markup does not resolve
#: theme variables (`$success`) inside rich text, only in CSS, so literal Rich
#: colour names are used here.
PENDING, RUNNING, DONE, FAILED, SKIPPED = "pending", "running", "done", "failed", "skipped"

_STATUS_MARKS = {
    PENDING: (ui.GLYPH_PENDING, "dim"),
    RUNNING: (ui.GLYPH_RUNNING, "bold cyan"),
    DONE: (ui.GLYPH_OK, "bold green"),
    FAILED: (ui.GLYPH_FAIL, "bold red"),
    SKIPPED: (ui.GLYPH_SKIPPED, "dim yellow"),
}


class StepLine(Static):
    """One line of the step list, with its status mark."""

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
        """The marked-up text, exactly as it is drawn.

        It exists so the tests can check what the line shows without depending
        on how Textual stores a `Static`'s content, which has changed between
        versions.
        """
        mark, style = _STATUS_MARKS[self._status]
        detail = f"  [dim]{self._detail}[/]" if self._detail else ""
        return f"[{style}]{mark}[/] [{self._index}/{self._total}] {self._title}{detail}"

    def _refresh(self) -> None:
        self.update(self.text)


class ConfigScreen(Screen):
    """Step 3 as a form: same content, same validation."""

    BINDINGS = [("escape", "app.quit", "Cancel")]

    def __init__(self, state: GlobalState) -> None:
        super().__init__()
        self._state = state
        #: The last validation error shown. It is what the tests check: the
        #: message is behaviour; how the `Static` paints it is not.
        self.last_error = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with VerticalScroll(id="form"):
            yield Static(self._environment_line(), id="environment-summary")

            yield Static("1 · Access", classes="section-title")
            with Vertical(classes="section"):
                yield Label("Access mode")
                yield Select(
                    [
                        ("LAN IP (self-signed certificate)", AccessMode.LAN.value),
                        ("Own domain (Let's Encrypt DNS-01)", AccessMode.DOMAIN.value),
                    ],
                    value=AccessMode.LAN.value,
                    id="access_mode",
                    allow_blank=False,
                )
                yield Label("Access IP or domain")
                yield Input(placeholder="192.168.1.50", id="host")

            yield Static("2 · Security", classes="section-title")
            with Vertical(classes="section"):
                yield Label("WireGuard panel username")
                yield Input(value=DEFAULT_PANEL_USERNAME, id="panel_username")
                yield Label("WireGuard panel password")
                yield Static(
                    f"At least {secret_utils.MIN_PANEL_PASSWORD_LENGTH} characters "
                    "(what wg-easy requires in order to let you in), and no "
                    + ", ".join(f"`{c}`" for c in secret_utils.FORBIDDEN_PASSWORD_CHAR_REASONS)
                    + ": they break the shell and .env files.",
                    classes="hint",
                )
                yield Input(password=True, id="panel_password")
                yield Label("Repeat the password")
                yield Input(password=True, id="panel_password_repeat")

            yield Static("3 · Model", classes="section-title")
            with Vertical(classes="section"):
                yield Label("AI model (name in Ollama)")
                yield Input(placeholder="qwen2.5:1.5b", id="model")

            yield Static("", id="error", classes="error")
            with Horizontal(id="actions"):
                # Starts disabled: the WireGuard variant is decided by step 1,
                # which runs in a threaded worker, and `on_mount` shows this
                # form without waiting for it. Anyone who filled it in and
                # pressed before detection finished got the default (`ports`)
                # — on native Linux, the wrong variant and a compose file that
                # does not match the platform.
                yield Button("Install", variant="primary", id="start", disabled=True)
                yield Button("Cancel", variant="error", id="cancel")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "AXION Wizard"
        self.sub_title = f"Configuration — {self._state.project_dir}"
        self.query_one("#host", Input).focus()
        # Detection may already have finished (cached environment, a fast
        # worker) before this screen was mounted.
        if getattr(self.app, "environment_ready", False):
            self.mark_environment_ready()

    def mark_environment_ready(self) -> None:
        """Enable submission once step 1 has decided the variant."""
        self.query_one("#start", Button).disabled = False
        self.query_one("#environment-summary", Static).update(self._environment_line())

    def _environment_line(self) -> str:
        """An echo of what step 1 decided, so the user is not left blind about
        what the wizard detected before the form appeared — the CLI shows it in
        a full table; here, in one line.

        Three states, and the first is the one that was missing: while
        detection is running nothing can be asserted, and showing the default
        as though it were a finding is worse than saying we are still looking.
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
        username = self.query_one("#panel_username", Input).value.strip()
        password = self.query_one("#panel_password", Input).value
        repeated = self.query_one("#panel_password_repeat", Input).value
        model = self.query_one("#model", Input).value.strip()

        if not host:
            raise ValueError("The host cannot be empty.")
        if not model:
            raise ValueError("Give the Ollama model to use.")
        if password != repeated:
            raise ValueError("The passwords do not match.")
        # The same rules as the CLI's prompt, and for the same reason:
        # `INIT_PASSWORD` does not validate length, so a short password
        # creates the panel account anyway and only fails later, on login.
        try:
            secret_utils.validate_wireguard_username(username)
            secret_utils.validate_wireguard_password(password)
        except (
            secret_utils.InvalidEnvValueError,
            secret_utils.ShortCredentialError,
        ) as exc:
            raise ValueError(str(exc)) from exc

        access_mode = AccessMode(str(self.query_one("#access_mode", Select).value))
        variant = self.app.detected_variant  # type: ignore[attr-defined]

        try:
            return AxionConfig(
                access_mode=access_mode,
                host=host,
                wireguard_variant=WireguardVariant(variant),
                # The password already written into `.env` is reused, if there
                # is one, rather than generating a new one. Postgres only
                # applies `POSTGRES_PASSWORD` when initialising its volume and
                # ignores it on every later start: a new password over an
                # already-initialised volume leaves Mattermost unable to
                # authenticate, with no error to explain it. The CLI path
                # already avoided this (see
                # `s03_config.existing_postgres_password`, which documents the
                # incident); this one did not, and reintroduced the same bug.
                postgres_password=SecretStr(
                    existing_postgres_password(self._state.project_dir)
                    or secret_utils.generate_hex_secret()
                ),
                wireguard_admin_username=username,
                wireguard_admin_password=SecretStr(password),
                ollama_model=model,
                project_dir=self._state.project_dir,
            )
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


class ProgressScreen(Screen):
    """The ten steps with their status, and the log below."""

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

    def log_renderable(self, renderable: RenderableType) -> None:
        """Dump a Rich renderable (a `Panel`, a `Table`) into the log.

        It is painted with the wizard's console first rather than handed
        straight to the `RichLog`: Textual would draw it with a `Console` of
        its own, which does not know the `axion.*` theme and blows up with
        `MissingStyle` as soon as the renderable uses `axion.label` (see
        `render.console.render_to_ansi`).

        Sharing the renderer is what stops the TUI's closing panel from
        becoming a hand-made copy of the CLI's, as it once was.
        """
        from rich.text import Text

        from axion_wizard.render.console import render_to_ansi

        log = self.query_one("#log", RichLog)
        width = max(log.size.width - 2, 40)
        log.write(Text.from_ansi(render_to_ansi(renderable, width=width)))


class AxionInstallerApp(App):
    """The root app: form → progress, with the steps in a worker.

    Its look leans on Textual's `nord` theme (see `APP_THEME`) plus this CSS,
    which only adds structure: grouping the form into sections with border
    titles, giving visual weight to the active section (`:focus-within`), and
    treating a validation error as a real alert — bordered, with a background
    — rather than a loose line of red text.
    """

    CSS = """
    Screen { background: $surface; }

    #form { padding: 1 2; }

    #environment-summary {
        color: $text-muted;
        margin-bottom: 1;
    }

    /* Deliberately light section headings: a label with a bottom rule, not a
    full box. One border per section (3 in this form) costs 2 rows of chrome
    each plus padding — in a 24-30 row terminal that pushes the buttons out of
    view without adding much that coloured text does not already give. */
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
        #: `True` as soon as step 1 has decided the variant. Until then the
        #: form will not submit: see `ConfigScreen`'s `#start` button.
        self.environment_ready = False
        self._install_context = InstallContext(project_dir=state.project_dir)
        self._install_steps: list = []
        self.theme = APP_THEME

    def on_mount(self) -> None:
        # Step 1 asks nothing and decides the variant the form needs, so it is
        # launched before showing it. The form appears immediately (waiting on
        # a black screen looks like a hang) but with submission blocked until
        # detection finishes.
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
        """Start steps 2-9 with the configuration already resolved."""
        from axion_wizard.steps import orchestrator

        self._install_context.config = config
        # Unattended on purpose: Textual owns the terminal, so no step may
        # open a questionary prompt underneath it.
        install_state = _quiet_copy(self._state, unattended=True)
        self._install_steps = orchestrator.build_steps(install_state, self._install_context)

        screen = ProgressScreen([step.title or step.name for step in self._install_steps])
        self.switch_screen(screen)
        self._run_steps(install_state)

    def _mark_resolved_steps_complete(self, install_state: GlobalState) -> None:
        """Take as done the steps the TUI has already resolved on its own.

        The environment was detected at startup and the configuration came
        from the form, so there is no need to run them again. They are marked
        in the *persisted state* rather than skipped with an `if` inside the
        loop — which is what used to happen — so the resume mechanism sees them
        exactly like any other step: on resume, `run_steps` calls their
        `restore()` and repopulates the context from disk.
        """
        from axion_wizard.utils import state as state_store

        if install_state.dry_run:
            return
        wizard_state = state_store.load_state(self._install_context.project_dir)
        for name, message in (
            ("environment", "resolved by the TUI before the form"),
            ("config", "filled in on the TUI form"),
        ):
            wizard_state.mark_complete(name, message)
        state_store.save_state(self._install_context.project_dir, wizard_state)

    @work(thread=True)
    def _run_steps(self, install_state: GlobalState) -> None:
        """Run the steps down the same path as the CLI.

        This worker used to have its own loop, copied from
        `orchestrator.run_steps` but without progress persistence: an
        interrupted `--tui` install did not resume, even though the README
        promises it does for `install`. It now delegates to the orchestrator
        and supplies only the reporter.
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

        from axion_wizard.steps import orchestrator

        screen.sub_title = "Complete" if all_ok else "Finished with errors"

        # The same closing panel the CLI prints, not a hand-made copy: where
        # to log in, with which model, and what warnings were left. It was
        # duplicated here, and the copy fell behind — it showed only three
        # lines, without the accumulated warnings, and only when everything had
        # gone well, which is precisely the opposite of when it is most needed
        # to know how to reach the parts that do work.
        panel = orchestrator.render_closing_summary(self._install_context, all_ok)
        if panel is not None:
            screen.log_line("")
            screen.log_renderable(panel)

        screen.log_line("[dim]Press `q` to quit.[/]")


class _TuiStepReporter:
    """Translate `orchestrator.run_steps` events onto the Textual screen.

    Everything goes through `call_from_thread` because the orchestrator runs
    in a worker on a separate thread: touching widgets from there directly is
    a race with the event loop.

    The orchestrator's indices are 1-based (they are for display, `[3/9]`) and
    `ProgressScreen`'s are 0-based (they are list positions); the conversion
    lives here and in exactly one place.
    """

    def __init__(self, app: AxionInstallerApp, screen: ProgressScreen) -> None:
        self._app = app
        self._screen = screen
        #: The last step started. When a step raises `AxionError`, the
        #: orchestrator re-raises without reaching `on_step_finished`, so
        #: without remembering it that line would spin on "running" forever —
        #: with the error printed right below it.
        self._current_index: int | None = None

    def _set(self, index: int, status: str, detail: str = "") -> None:
        self._app.call_from_thread(self._screen.set_step, index - 1, status, detail)

    def _log(self, text: str) -> None:
        self._app.call_from_thread(self._screen.log_line, text)

    def on_step_resumed(self, index: int, total: int, step: object) -> None:
        self._set(index, DONE, "already done, resuming")

    def on_step_invalidated(self, index: int, total: int, step: object, reason: str) -> None:
        self._set(index, PENDING, f"redoing: {reason}")
        self._log(f"[yellow]Redoing step {index}:[/] {reason}")

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
        """The CLI's error panel, taken apart into lines for the log."""
        if self._current_index is not None:
            self._set(self._current_index, FAILED, exc.what)
            self._current_index = None
        self._log(f"[bold red]{exc.what}[/]\n{exc.why}")
        for number, action in enumerate(exc.steps, start=1):
            self._log(f"  {number}. {action}")


def _quiet_copy(state: GlobalState, **overrides: object) -> GlobalState:
    """A copy of `GlobalState` with console output silenced.

    The steps print with Rich onto stdout, and Textual owns the screen:
    without silencing them, their tables and panels bleed over the interface
    and leave it unreadable.
    """
    from dataclasses import replace

    return replace(state, quiet=True, **overrides)  # type: ignore[arg-type]
