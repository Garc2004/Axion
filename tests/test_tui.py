"""Tests for the Textual interface (`install --tui`).

Driven with Textual's `run_test()`, which starts the app against a headless
driver: without it a real terminal would be required and there would be no
way to cover this in CI.
"""

from pathlib import Path

import pytest
from pydantic import SecretStr

from axion_wizard.cli import GlobalState
from axion_wizard.domain.config import WireguardVariant
from axion_wizard.errors import ConfigError
from axion_wizard.steps import orchestrator

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


#: A roomy terminal for the tests: at the default size the "Install" button
#: falls outside the visible region and `pilot.click` rejects it with
#: OutOfBounds — an artefact of the harness, not of the form.
TERMINAL_SIZE = (120, 50)


def _app(tmp_path: Path, mocker, *, environment_ready: bool = True):
    """The app with step 1 (detection) stubbed out: it starts real subprocesses.

    `environment_ready` simulates that detection having finished — which is
    what the real worker does on completion, and without it the form keeps
    submission blocked on purpose (see `ConfigScreen`'s `#start` button).
    """
    from axion_wizard.tui.app import AxionInstallerApp

    app = AxionInstallerApp(GlobalState(project_dir=tmp_path))
    mocker.patch.object(app, "_detect_environment")
    app.detected_variant = WireguardVariant.PORTS.value
    app.environment_ready = environment_ready
    return app


# --- `--tui` guards ---------------------------------------------------------------


def test_tui_rejects_unattended(tmp_path: Path) -> None:
    """Textual exists to fill in a form by hand; with nobody in front of it,
    it would sit waiting for keystrokes, which from outside looks like a
    hang."""
    from axion_wizard.commands.install import _assert_tui_is_usable

    with pytest.raises(ConfigError, match="unattended"):
        _assert_tui_is_usable(GlobalState(project_dir=tmp_path), unattended=True)


def test_tui_rejects_a_non_interactive_stdin(tmp_path: Path, mocker) -> None:
    from axion_wizard.commands.install import _assert_tui_is_usable

    fake_stdin = mocker.Mock()
    fake_stdin.isatty.return_value = False
    mocker.patch("sys.stdin", fake_stdin)

    with pytest.raises(ConfigError, match="interactive terminal"):
        _assert_tui_is_usable(GlobalState(project_dir=tmp_path), unattended=False)


# --- formulario ---------------------------------------------------------------------


async def test_form_reports_mismatched_passwords(tmp_path: Path, mocker) -> None:
    from textual.widgets import Input

    app = _app(tmp_path, mocker)
    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.query_one("#host", Input).value = "192.168.1.50"
        screen.query_one("#model", Input).value = "qwen2.5:1.5b"
        screen.query_one("#panel_password", Input).value = "contrasena-1"
        screen.query_one("#panel_password_repeat", Input).value = "contrasena-2"
        await pilot.click("#start")
        await pilot.pause()

        assert "do not match" in screen.last_error


async def test_form_rejects_a_forbidden_character(tmp_path: Path, mocker) -> None:
    """The same rule as the questionary prompt: `$` is rejected up front, with
    the reason in plain sight."""
    from textual.widgets import Input

    app = _app(tmp_path, mocker)
    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.query_one("#host", Input).value = "192.168.1.50"
        screen.query_one("#model", Input).value = "qwen2.5:1.5b"
        screen.query_one("#panel_password", Input).value = "has$a-dollar"
        screen.query_one("#panel_password_repeat", Input).value = "has$a-dollar"
        await pilot.click("#start")
        await pilot.pause()

        assert "$" in screen.last_error


async def test_form_requires_a_host(tmp_path: Path, mocker) -> None:
    from textual.widgets import Input

    app = _app(tmp_path, mocker)
    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.query_one("#model", Input).value = "qwen2.5:1.5b"
        screen.query_one("#panel_password", Input).value = "contrasena-buena"
        screen.query_one("#panel_password_repeat", Input).value = "contrasena-buena"
        await pilot.click("#start")
        await pilot.pause()

        assert "host" in screen.last_error.lower()


async def test_a_valid_form_builds_the_config_and_starts(tmp_path: Path, mocker) -> None:
    from textual.widgets import Input

    app = _app(tmp_path, mocker)
    started = mocker.patch.object(app, "begin_install")

    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.query_one("#host", Input).value = "192.168.1.50"
        screen.query_one("#model", Input).value = "qwen2.5:1.5b"
        screen.query_one("#panel_password", Input).value = "contrasena-buena"
        screen.query_one("#panel_password_repeat", Input).value = "contrasena-buena"
        await pilot.click("#start")
        await pilot.pause()

    started.assert_called_once()
    config = started.call_args[0][0]
    assert config.host == "192.168.1.50"
    assert config.ollama_model == "qwen2.5:1.5b"
    # The password is masked in any output; the config keeps it as a SecretStr (§9).
    assert config.wireguard_admin_username == "admin"
    assert config.wireguard_admin_password.get_secret_value() == "contrasena-buena"
    assert config.postgres_password.get_secret_value() != "contrasena-buena"


async def test_the_form_never_renders_the_password(tmp_path: Path, mocker) -> None:
    from textual.widgets import Input

    app = _app(tmp_path, mocker)
    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await pilot.pause()
        password_input = app.screen.query_one("#panel_password", Input)
        password_input.value = "contrasena-secreta"
        await pilot.pause()
        assert password_input.password is True


# --- design: vocabulary shared with the CLI, and the form's structure ---------------------


def test_app_uses_the_nord_theme(tmp_path: Path, mocker) -> None:
    """Visual coherence: a palette that was designed, not hex colours invented
    by hand, and the same one across the whole app (it does not change between
    screens)."""
    from axion_wizard.tui.app import APP_THEME

    app = _app(tmp_path, mocker)
    assert app.theme == APP_THEME


def test_step_status_glyphs_come_from_the_shared_ui_module(tmp_path: Path, mocker) -> None:
    """A ✓ in `doctor` (CLI) and a ✓ in `install --tui` being the same
    character is no accident: both read `axion_wizard.render.ui.GLYPH_*`."""
    from axion_wizard.render import ui
    from axion_wizard.tui.app import _STATUS_MARKS, DONE, FAILED, PENDING, RUNNING, SKIPPED

    assert _STATUS_MARKS[DONE][0] == ui.GLYPH_OK
    assert _STATUS_MARKS[FAILED][0] == ui.GLYPH_FAIL
    assert _STATUS_MARKS[PENDING][0] == ui.GLYPH_PENDING
    assert _STATUS_MARKS[RUNNING][0] == ui.GLYPH_RUNNING
    assert _STATUS_MARKS[SKIPPED][0] == ui.GLYPH_SKIPPED


async def test_form_is_grouped_into_titled_sections(tmp_path: Path, mocker) -> None:
    """Design regression: the form's three sections (Access, Security, Model)
    must still be present and in order — it is what makes a five-field form
    scannable rather than a flat list."""
    from textual.widgets import Static

    app = _app(tmp_path, mocker)
    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await pilot.pause()
        rendered = [
            str(s.content) for s in app.screen.query(Static) if "section-title" in s.classes
        ]
        assert rendered == ["1 · Access", "2 · Security", "3 · Model"]


async def test_error_banner_is_hidden_until_there_is_an_error(tmp_path: Path, mocker) -> None:
    """The error banner is a bordered box, not a loose line of text — but only
    when there is something to show: always reserving the space would leave an
    ugly gap in a form already tight for room."""
    from textual.widgets import Input, Static

    app = _app(tmp_path, mocker)
    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await pilot.pause()
        screen = app.screen
        error = screen.query_one("#error", Static)
        assert "visible" not in error.classes

        screen.query_one("#host", Input).value = "192.168.1.50"
        screen.query_one("#model", Input).value = "qwen2.5:1.5b"
        screen.query_one("#panel_password", Input).value = "contrasena-1"
        screen.query_one("#panel_password_repeat", Input).value = "contrasena-2"
        await pilot.click("#start")
        await pilot.pause()

        assert "visible" in error.classes

        screen.query_one("#panel_password_repeat", Input).value = "contrasena-1"
        await pilot.click("#start")
        await pilot.pause()


async def test_config_screen_shows_the_detected_environment(tmp_path: Path, mocker) -> None:
    """An echo of what step 1 decided: the CLI shows it in a full table, the
    TUI in one line — but it must not leave the user blind about what the
    wizard detected before they fill in the form."""
    from axion_wizard.domain.config import WireguardVariant

    app = _app(tmp_path, mocker)
    app.detected_variant = WireguardVariant.HOST.value
    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await pilot.pause()
        summary = app.screen.query_one("#environment-summary")
        assert "host" in str(summary.content)


# --- progress screen ------------------------------------------------------------------


async def test_progress_screen_lists_every_step(tmp_path: Path, mocker) -> None:
    from axion_wizard.tui.app import ProgressScreen, StepLine

    app = _app(tmp_path, mocker)
    titles = ["uno", "dos", "tres"]
    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await app.push_screen(ProgressScreen(titles))
        await pilot.pause()
        assert len(app.screen.query(StepLine)) == len(titles)


async def test_progress_screen_sections_have_titled_borders(tmp_path: Path, mocker) -> None:
    """`border_title` is a widget property, not content text: easy to set by
    accident on a widget that, in the app's real tree, has no CSS with
    `border:` to draw it — verified here against the real app
    (`AxionInstallerApp`), not a generic test `App`."""
    from axion_wizard.tui.app import ProgressScreen

    app = _app(tmp_path, mocker)
    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        screen = ProgressScreen(["uno"])
        await app.push_screen(screen)
        await pilot.pause()

        steps = screen.query_one("#steps")
        log = screen.query_one("#log")
        assert steps.border_title == "Progreso"
        assert log.border_title == "Registro"
        assert steps.styles.border.top[0] != ""
        assert log.styles.border.top[0] != ""


async def test_step_line_shows_its_status(tmp_path: Path, mocker) -> None:
    from axion_wizard.tui.app import DONE, FAILED, ProgressScreen, StepLine

    app = _app(tmp_path, mocker)
    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        screen = ProgressScreen(["uno", "dos"])
        await app.push_screen(screen)
        await pilot.pause()

        screen.set_step(0, DONE, "done")
        screen.set_step(1, FAILED, "blew up")
        await pilot.pause()

        lines = list(app.screen.query(StepLine))
        assert "done" in lines[0].text
        assert "blew up" in lines[1].text


# --- silenced state ------------------------------------------------------------------


def test_steps_run_quiet_and_unattended_under_the_tui(tmp_path: Path) -> None:
    """The steps print with Rich onto stdout; without silencing them, their
    tables bleed over the interface. And none of them may open a questionary
    prompt while Textual owns the screen."""
    from axion_wizard.tui.app import _quiet_copy

    state = GlobalState(project_dir=tmp_path, verbose=True)
    quiet = _quiet_copy(state, unattended=True)

    assert quiet.quiet is True
    assert quiet.unattended is True
    assert quiet.project_dir == tmp_path
    assert state.quiet is False, "the original must not be mutated"


# --- the PostgreSQL password is not regenerated -------------------------------------
#
# Regression: the TUI's form generated a new password on every run. Postgres
# only applies POSTGRES_PASSWORD when initialising its volume and ignores it
# afterwards, so on an already-deployed project Mattermost stopped
# authenticating with no error to explain it. The CLI path already avoided this
# (s03_config.existing_postgres_password documents the incident).


async def test_tui_reuses_the_existing_postgres_password(tmp_path: Path, mocker) -> None:
    from textual.widgets import Input

    (tmp_path / ".env").write_text(
        "POSTGRES_PASSWORD=deadbeefcafe1234\nOLLAMA_MODEL=qwen2.5:1.5b\n", encoding="utf-8"
    )
    app = _app(tmp_path, mocker)
    captured = {}
    mocker.patch.object(app, "begin_install", side_effect=lambda config: captured.update(c=config))

    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.query_one("#host", Input).value = "192.168.1.50"
        screen.query_one("#model", Input).value = "qwen2.5:1.5b"
        screen.query_one("#panel_password", Input).value = "contrasena-larga"
        screen.query_one("#panel_password_repeat", Input).value = "contrasena-larga"
        await pilot.click("#start")
        await pilot.pause()

    assert captured["c"].postgres_password.get_secret_value() == "deadbeefcafe1234"


async def test_tui_generates_a_password_when_there_is_no_previous_env(
    tmp_path: Path, mocker
) -> None:
    from textual.widgets import Input

    app = _app(tmp_path, mocker)
    captured = {}
    mocker.patch.object(app, "begin_install", side_effect=lambda config: captured.update(c=config))

    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.query_one("#host", Input).value = "192.168.1.50"
        screen.query_one("#model", Input).value = "qwen2.5:1.5b"
        screen.query_one("#panel_password", Input).value = "contrasena-larga"
        screen.query_one("#panel_password_repeat", Input).value = "contrasena-larga"
        await pilot.click("#start")
        await pilot.pause()

    assert len(captured["c"].postgres_password.get_secret_value()) == 64


# --- the form cannot be submitted before the environment is detected ----------------
#
# `on_mount` launches detection in a threaded worker and shows the form without
# waiting for it. Anyone who filled it in and pressed before it finished got
# the variant's default (`ports`) — on native Linux, the wrong one.


async def test_install_is_blocked_until_the_environment_is_detected(
    tmp_path: Path, mocker
) -> None:
    from textual.widgets import Button

    app = _app(tmp_path, mocker, environment_ready=False)
    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await pilot.pause()
        assert app.screen.query_one("#start", Button).disabled is True
        assert "Detectando" in str(app.screen.query_one("#environment-summary").content)


async def test_install_unblocks_once_detection_finishes(tmp_path: Path, mocker) -> None:
    from textual.widgets import Button

    app = _app(tmp_path, mocker, environment_ready=False)
    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await pilot.pause()
        app.environment_ready = True
        app.screen.mark_environment_ready()
        await pilot.pause()
        assert app.screen.query_one("#start", Button).disabled is False


# --- parity with the CLI: progress is persisted -------------------------------------
#
# The TUI's worker had its own step loop, copied from the orchestrator but
# without the persistence: an interrupted `--tui` install did not resume, even
# though the README promises it does for `install`.


def test_tui_marks_the_steps_it_resolved_itself_as_complete(tmp_path: Path, mocker) -> None:
    from axion_wizard.tui.app import AxionInstallerApp
    from axion_wizard.utils import state as state_store

    app = AxionInstallerApp(GlobalState(project_dir=tmp_path))
    app._mark_resolved_steps_complete(GlobalState(project_dir=tmp_path))

    persisted = state_store.load_state(tmp_path)
    assert persisted.is_complete("environment") is True
    assert persisted.is_complete("config") is True


def test_tui_does_not_persist_anything_on_dry_run(tmp_path: Path) -> None:
    from axion_wizard.tui.app import AxionInstallerApp
    from axion_wizard.utils import state as state_store

    app = AxionInstallerApp(GlobalState(project_dir=tmp_path))
    app._mark_resolved_steps_complete(GlobalState(project_dir=tmp_path, dry_run=True))

    assert not state_store.state_path(tmp_path).exists()


# --- closing panel: one only, shared with the CLI ------------------------------------


async def test_tui_closing_summary_comes_from_the_shared_renderer(
    tmp_path: Path, mocker
) -> None:
    """The TUI's closing panel is the same object the CLI prints.

    It was duplicated by hand: three lines rewritten with their own markup,
    without the accumulated warnings and only when everything had gone well. A
    copy that silently falls behind is worse than no copy at all, because it
    looks as though both interfaces say the same thing. The delegation is
    asserted, not the text, so this test keeps its value when the panel's
    content changes.
    """
    from axion_wizard.domain.config import AccessMode, AxionConfig, WireguardVariant
    from axion_wizard.tui.app import ProgressScreen

    app = _app(tmp_path, mocker)
    app._install_context.config = AxionConfig(
        access_mode=AccessMode.LAN,
        host="192.168.1.50",
        wireguard_variant=WireguardVariant.PORTS,
        postgres_password=SecretStr("a" * 64),
        wireguard_admin_username="admin",
        wireguard_admin_password=SecretStr("correct-horse-battery-staple"),
        ollama_model="qwen2.5:3b",
        project_dir=tmp_path,
    )
    app._install_context.warn("something was left half-done")

    render = mocker.patch(
        "axion_wizard.steps.orchestrator.render_closing_summary",
        wraps=orchestrator.render_closing_summary,
    )

    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await app.push_screen(ProgressScreen(["uno"]))
        await pilot.pause()
        app._finish(all_ok=True)
        await pilot.pause()

    render.assert_called_once_with(app._install_context, True)
