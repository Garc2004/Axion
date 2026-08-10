"""The Typer app and its subcommand registry."""

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
    help="Installer/orchestrator for the AXION stack (Mattermost + WireGuard + Ollama + FastAPI).",
    no_args_is_help=False,
    add_completion=True,
    pretty_exceptions_enable=False,
)


@dataclass
class GlobalState:
    """Global options shared by every subcommand."""

    verbose: bool = False
    quiet: bool = False
    no_color: bool = False
    dry_run: bool = False
    yes: bool = False
    no_elevate: bool = False
    project_dir: Path = field(default_factory=Path.cwd)
    #: `install --unattended`: forbids any prompt. The steps consult it so
    #: they do not sit waiting for an answer that never arrives.
    unattended: bool = False
    #: `install --config`: a TOML file with the complete configuration.
    config_path: Path | None = None


state = GlobalState()

#: The subcommands that genuinely need privileges, as an explicit list.
#:
#: An allowlist and not a denylist on purpose: §9 asks for elevation only at
#: the points that require it, so the safe default is *not* to elevate and to
#: name the exceptions. With the list inverted, every new subcommand would
#: inherit elevation by accident — which is exactly what happened to
#: `gen-cert`, which only writes a couple of files.
#:
#: `None` stands for the default flow (`axion-wizard` with no subcommand),
#: which is equivalent to `install`.
#:
#: The list is documentation and an anchor for the tests; what actually
#: elevates is each command's `_dispatch(..., elevate=True)`.
ELEVATION_REQUIRED_COMMANDS: frozenset[str | None] = frozenset(
    {None, "install", "up", "down", "uninstall", "set-webhook-token", "set-bot-token"}
)


def _render_error(exc: AxionError) -> None:
    body = Text()
    body.append(f"{exc.what}\n\n", style="bold")
    body.append(f"{exc.why}\n\n")
    if exc.steps:
        body.append("What to do:\n", style="axion.heading")
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
    """Run a command's body, turning `AxionError` into a Rich panel plus a
    non-zero exit. It is invoked from each command (not only from `main()`)
    because Click/Typer do not route subcommand exceptions through `main()`'s
    wrapper when `app` is invoked directly (under `CliRunner` in the tests,
    for instance).

    `elevate=True` asks for privileges *immediately before* running the body,
    rather than from the group callback, which is where it used to live. Click
    runs that callback before processing a subcommand's `--help`, so
    `axion-wizard install --help` opened a UAC prompt — and, now that it waits
    on the elevated process, blocked — merely to read the help text. From here
    that can no longer happen: Click prints the help and exits without ever
    invoking the command body.
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
    """Make sure we are running with administrator privileges, relaunching the
    process if necessary.

    Only the commands in `ELEVATION_REQUIRED_COMMANDS` invoke it, via
    `_dispatch(..., elevate=True)`; never under `--no-elevate` and never under
    `--dry-run`, which by definition touches nothing. Nor does it elevate
    silently: it says what it needs it for first.

    `--project-dir` is passed to the child explicitly because on Windows it
    does not inherit the parent's working directory (see
    `privileges.relaunch_elevated_windows`), and without it the child would
    fall back to its default, `Path.cwd()`, which for a UAC-launched process
    is `C:\\Windows\\System32`.
    """
    if state.no_elevate or state.dry_run:
        return
    if privileges.is_elevated():
        return

    console.print(f"[axion.warn]{privileges.explain_elevation_reason()}[/]")
    console.print(
        "[axion.dim]Relaunching with elevated privileges… "
        "(use --no-elevate to continue without them)[/]"
    )

    project_dir = str(state.project_dir)
    try:
        exit_code = privileges.relaunch_elevated(
            leading_args=["--project-dir", project_dir],
            working_dir=project_dir,
        )
    except privileges.ElevationError as exc:
        raise PlatformError(
            what="Administrator privileges could not be obtained",
            why=str(exc),
            steps=[
                "Accept the UAC prompt (Windows) or enter the sudo password (Linux).",
                "Open the terminal as Administrator/root and retry.",
                "Or carry on unelevated with --no-elevate (some steps may fail).",
            ],
        ) from exc

    # The elevated child runs in its own window and already pauses before
    # closing it; having the parent pause too would mean pressing Enter twice,
    # in two different windows, for a single run.
    winconsole.disable_pause()
    raise typer.Exit(code=exit_code)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"axion-wizard {__version__}")
        raise typer.Exit()


#: Subdirectory deployed into by default when `--project-dir` is not given
#: and there is no deployment in the current directory already.
DEFAULT_PROJECT_SUBDIR = "axion"


def _default_project_dir() -> Path:
    """The project directory when `--project-dir` was not given (§4.2).

    Without this, running the binary exactly as downloaded — double-clicked
    from `~/Downloads`, say — wrote `docker-compose.yml`, `.env`, `nginx/`,
    `backups/`… loose in there, mixed in with whatever else was present. A
    real incident, not a hypothetical one.

    If there is already a deployment in the current directory
    (`docker-compose.yml` present — the case of someone who followed the
    README's advice to create a dedicated folder and put the binary inside) it
    is used as-is, without nesting one more folder: it is already "inside a
    folder".
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
        help="Show the version and exit.",
    ),
    verbose: bool = typer.Option(False, "--verbose", help="Verbose output, tracebacks included."),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress non-essential output."),
    no_color: bool = typer.Option(False, "--no-color", help="Disable colour in the output."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print every command and file write without running them."
    ),
    yes: bool = typer.Option(False, "--yes", help="Assume 'yes' to every confirmation."),
    no_elevate: bool = typer.Option(
        False,
        "--no-elevate",
        help="Do not ask for administrator privileges (some steps may fail).",
    ),
    project_dir: Path | None = typer.Option(
        None, "--project-dir", help="AXION project directory.", file_okay=False
    ),
) -> None:
    state.verbose = verbose
    state.quiet = quiet
    state.no_color = no_color
    state.dry_run = dry_run
    state.yes = yes
    state.no_elevate = no_elevate
    # Absolute from the start: the elevated relaunch begins in a different
    # working directory (see `ensure_elevated`), so a relative path such as
    # `--project-dir ./axion` would point somewhere else in the child.
    used_default_subdir = project_dir is None and not (Path.cwd() / "docker-compose.yml").exists()
    resolved_project_dir = project_dir if project_dir is not None else _default_project_dir()
    state.project_dir = resolved_project_dir.resolve()

    set_quiet(quiet)
    set_no_color(no_color)

    if used_default_subdir and not quiet:
        console.print(
            f"[axion.dim]No --project-dir given: {state.project_dir} will be used "
            "(pass it explicitly to choose another folder).[/]"
        )

    if ctx.invoked_subcommand is None:
        from axion_wizard.commands import run_install

        _dispatch(lambda: run_install(state), elevate=True)


@app.command()
def install(
    unattended: bool = typer.Option(
        False, "--unattended", help="No interactive prompts; requires --config."
    ),
    config: Path | None = typer.Option(
        None, "--config", help="An axion.toml file with the complete configuration."
    ),
    tui: bool = typer.Option(
        False, "--tui", help="Full-screen interface instead of sequential prompts."
    ),
    restart: bool = typer.Option(
        False,
        "--restart",
        help="Ignore saved progress and redo the install from step 1.",
    ),
) -> None:
    """Run the complete install flow."""
    from axion_wizard.commands import run_install

    _dispatch(
        lambda: run_install(
            state, unattended=unattended, config_path=config, tui=tui, restart=restart
        ),
        elevate=True,
    )


@app.command()
def reset(
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
) -> None:
    """Forget the progress so `install` starts again at step 1.

    It deletes no containers, volumes, `.env` or certificate — only the record
    of which steps had been completed.
    """
    from axion_wizard.commands import run_reset

    _dispatch(lambda: run_reset(state, yes=yes))


@app.command()
def doctor() -> None:
    """Re-validate the whole deployed stack without modifying it."""
    from axion_wizard.commands import run_doctor

    _dispatch(lambda: run_doctor(state))


@app.command(name="network-check")
def network_check() -> None:
    """Run only the network checks (§4.2)."""
    from axion_wizard.commands import run_network_check

    _dispatch(lambda: run_network_check(state))


@app.command(name="gen-cert")
def gen_cert(
    host: str = typer.Argument(..., help="IP or domain for the certificate subjectAltName."),
) -> None:
    """Generate only the TLS certificate."""
    from axion_wizard.commands import run_gen_cert

    _dispatch(lambda: run_gen_cert(state, host=host))


@app.command(name="set-webhook-token")
def set_webhook_token(
    token: str = typer.Argument(
        ..., help="Outgoing webhook token, generated by Mattermost when it is created."
    ),
) -> None:
    """Store Mattermost's webhook token and recreate fastapi to use it."""
    from axion_wizard.commands import run_set_webhook_token

    _dispatch(lambda: run_set_webhook_token(state, token=token), elevate=True)


@app.command(name="set-bot-token")
def set_bot_token(
    token: str = typer.Argument(..., help="A Mattermost bot token."),
) -> None:
    """Let the AI take as long as it needs: it answers in the channel when done.

    Without this, Mattermost abandons the webhook request after ~30 seconds
    and a slow model's answer is lost whole, with no visible error.
    """
    from axion_wizard.commands import run_set_bot_token

    _dispatch(lambda: run_set_bot_token(state, token=token), elevate=True)


models_app = typer.Typer(help="Ollama model catalogue.")
app.add_typer(models_app, name="models")


@models_app.callback(invoke_without_command=True)
def models_list(ctx: typer.Context) -> None:
    """List Ollama models compatible with the detected hardware."""
    if ctx.invoked_subcommand is None:
        from axion_wizard.commands import run_models_list

        _dispatch(lambda: run_models_list(state))


@models_app.command("pull")
def models_pull(name: str = typer.Argument(..., help="Name of the model to download.")) -> None:
    """Download an Ollama model."""
    from axion_wizard.commands import run_models_pull

    _dispatch(lambda: run_models_pull(state, name=name))


model_app = typer.Typer(
    help=(
        "Edit the AI: which model it uses and with what instructions. Writes .env "
        "and recreates the fastapi container for you."
    )
)
app.add_typer(model_app, name="model")


@model_app.callback(invoke_without_command=True)
def model_show(ctx: typer.Context) -> None:
    """Show the model and instructions the AI is using right now."""
    if ctx.invoked_subcommand is None:
        from axion_wizard.commands import run_model_show

        _dispatch(lambda: run_model_show(state))


@model_app.command("set")
def model_set(
    name: str = typer.Argument(..., help="Ollama model the AI should use."),
    no_pull: bool = typer.Option(
        False, "--no-pull", help="Do not download it if missing (it will fail until present)."
    ),
) -> None:
    """Change the AI's model: download it, write it into .env and recreate fastapi."""
    from axion_wizard.commands import run_model_set

    _dispatch(lambda: run_model_set(state, name=name, skip_pull=no_pull))


@model_app.command("choose")
def model_choose() -> None:
    """Pick the model from a list ordered by the detected hardware."""
    from axion_wizard.commands import run_model_choose

    _dispatch(lambda: run_model_choose(state))


@model_app.command("prompt")
def model_prompt(
    text: str = typer.Argument(
        ...,
        help=(
            "The AI's standing instructions (tone, language, what it is). "
            'An empty string ("") clears them.'
        ),
    ),
) -> None:
    """Edit the AI's standing instructions."""
    from axion_wizard.commands import run_model_prompt

    _dispatch(lambda: run_model_prompt(state, prompt=text))


wireguard_app = typer.Typer(help="WireGuard panel management (wg-easy).")
app.add_typer(wireguard_app, name="wireguard")


@wireguard_app.command("add-client")
def wireguard_add_client(
    name: str = typer.Argument(..., help="Name of the client to create."),
) -> None:
    """Create a WireGuard client and show its QR in the terminal."""
    from axion_wizard.commands import run_wireguard_add_client

    _dispatch(lambda: run_wireguard_add_client(state, name=name))


@app.command()
def up(
    service: str | None = typer.Argument(None, help="Service to bring up (all if omitted)."),
) -> None:
    """Shortcut for `docker compose up -d`."""
    from axion_wizard.commands import run_compose_up

    _dispatch(lambda: run_compose_up(state, service=service), elevate=True)


@app.command()
def down() -> None:
    """Shortcut for `docker compose down`."""
    from axion_wizard.commands import run_compose_down

    _dispatch(lambda: run_compose_down(state), elevate=True)


@app.command()
def logs(
    service: str | None = typer.Argument(None, help="Service whose logs to show."),
) -> None:
    """Show the last lines of each service's log (it does not follow the log)."""
    from axion_wizard.commands import run_compose_logs

    _dispatch(lambda: run_compose_logs(state, service=service))


@app.command()
def uninstall(
    purge: bool = typer.Option(False, "--purge", help="Delete the data volumes too."),
) -> None:
    """Bring the AXION stack down."""
    from axion_wizard.commands import run_uninstall

    _dispatch(lambda: run_uninstall(state, purge=purge), elevate=True)


def main() -> None:
    """The executable's entry point.

    The `finally` is not decorative: when the wizard is opened by
    double-clicking from Explorer — or when it is the child UAC has just
    relaunched — the console was created for this process and Windows destroys
    it on exit. Without the pause, any output (the error panel just printed
    included) vanishes before it can be read and the launch looks like a
    crash. `winconsole` decides on its own whether pausing is appropriate; in
    a normal terminal, in a pipe, or in CI it does nothing.
    """
    try:
        try:
            app()
        except AxionError as exc:
            _render_error(exc)
            if state.verbose:
                error_console.print_exception()
            raise SystemExit(1) from None
        except Exception as exc:  # noqa: BLE001 - last resort; never a raw traceback without --verbose
            error_console.print(f"[axion.error]Unexpected error:[/] {exc}")
            if state.verbose:
                error_console.print_exception()
            raise SystemExit(1) from None
    finally:
        winconsole.pause_if_console_would_close()


if __name__ == "__main__":
    main()
