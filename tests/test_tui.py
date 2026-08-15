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


async def _press_install(pilot) -> None:
    """Scroll the Install button into view, then press it.

    `#form` is a `VerticalScroll`, and with the model catalogue shown the form
    is taller than a short terminal — so `pilot.click` can report the button as
    out of bounds where a real user simply scrolls. Scrolling first is what
    that user does, not a way around the harness.

    `animate=False` matters: the animated scroll had not finished by the next
    `pause()`, so the button was still half off-screen when the click landed.
    """
    pilot.app.screen.query_one("#start").scroll_visible(animate=False)
    await pilot.pause()
    await pilot.click("#start")
    await pilot.pause()


async def _submit_and_confirm(pilot) -> None:
    """Press Install on the form, then again on the confirmation screen.

    The form no longer starts the deployment on its own: it shows the summary
    first and waits, the same single confirmation step 3 of the CLI ends with.
    Tests that only care about the resulting config still have to pass through
    it, because that is now the real path.
    """
    await _press_install(pilot)
    await pilot.click("#confirm-install")
    await pilot.pause()


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


# --- the form ------------------------------------------------------------------------


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
        await _submit_and_confirm(pilot)

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
    titles = ["one", "two", "three"]
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
        screen = ProgressScreen(["one"])
        await app.push_screen(screen)
        await pilot.pause()

        steps = screen.query_one("#steps")
        log = screen.query_one("#log")
        assert steps.border_title == "Progress"
        assert log.border_title == "Log"
        assert steps.styles.border.top[0] != ""
        assert log.styles.border.top[0] != ""


async def test_step_line_shows_its_status(tmp_path: Path, mocker) -> None:
    from axion_wizard.tui.app import DONE, FAILED, ProgressScreen, StepLine

    app = _app(tmp_path, mocker)
    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        screen = ProgressScreen(["one", "two"])
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
        await _submit_and_confirm(pilot)

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
        await _submit_and_confirm(pilot)

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
        assert "Detecting" in str(app.screen.query_one("#environment-summary").content)


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
        await app.push_screen(ProgressScreen(["one"]))
        await pilot.pause()
        app._finish(all_ok=True)
        await pilot.pause()

    render.assert_called_once_with(app._install_context, True)


# --- the model picker: the same catalogue the CLI offers ---------------------------
#
# The form used to carry a bare `Input` with a placeholder, so choosing the
# model — the one decision in this form that depends on the detected hardware —
# was made blind here and informed on the CLI. These cover that it is now the
# same §5 catalogue, and that it degrades to free text rather than to nothing.


def _catalog():
    from axion_wizard.services.ollama import ModelInfo

    return [
        ModelInfo(name="qwen2.5:1.5b", size_bytes=1 << 30, min_ram_gb=4, needs_gpu=False),
        ModelInfo(
            name="qwen2.5:3b", size_bytes=2 << 30, min_ram_gb=8, needs_gpu=False, installed=True
        ),
        ModelInfo(name="llama3.1:70b", size_bytes=40 << 30, min_ram_gb=64, needs_gpu=True),
    ]


async def _detection_finishes(app, pilot, catalog=None, recommended=None) -> None:
    """Put the app in the state the detection worker leaves behind.

    `_app` stubs out `_detect_environment` because it starts real subprocesses,
    so the catalogue and the environment facts it would have produced are
    supplied here instead. The facts are complete rather than half-`None`:
    `mark_environment_ready` also redraws the environment line, which reads
    the OS and Docker versions.
    """
    from axion_wizard.detect.docker import DockerContextInfo, DockerInfo
    from axion_wizard.detect.hardware import HardwareInfo
    from axion_wizard.detect.platform import OsInfo, WslInfo
    from axion_wizard.steps.context import EnvironmentFacts

    app.model_catalog = _catalog() if catalog is None else catalog
    app.recommended_model = recommended
    app._install_context.environment = EnvironmentFacts(
        os_info=OsInfo(name="Windows", release="11"),
        wsl=WslInfo(inside_wsl=False),
        docker=DockerInfo(
            installed=True,
            docker_version="28.0",
            compose_version="2.30",
            compose_is_v2=True,
            context=DockerContextInfo(active_context="default", is_desktop=True),
        ),
        hardware=HardwareInfo(ram_total_bytes=16 * 1024**3, cpu_logical=8, cpu_physical=4),
        wireguard_variant=WireguardVariant.PORTS.value,
    )
    app.screen.mark_environment_ready()
    await pilot.pause()


async def test_the_catalogue_is_offered_once_detection_finishes(
    tmp_path: Path, mocker
) -> None:
    """The list is the CLI's, drawn by the same `describe_model`: name, size
    and how it fits the detected hardware."""
    from textual.widgets import OptionList

    app = _app(tmp_path, mocker)
    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await pilot.pause()
        await _detection_finishes(app, pilot)

        option_list = app.screen.query_one("#model-catalog", OptionList)
        assert option_list.display is True
        rendered = " ".join(str(o.prompt) for o in option_list._options)
        assert "qwen2.5:1.5b" in rendered
        # The catalogue's judgement, not just its names: size and fit are the
        # whole reason for showing a list rather than a text box.
        assert "GB" in rendered
        assert "already installed" in rendered
        assert "needs a dedicated GPU" in rendered


async def test_picking_from_the_list_fills_the_field(tmp_path: Path, mocker) -> None:
    """The `Input` is the single source of truth: selecting writes into it."""
    from textual.widgets import Input, OptionList

    app = _app(tmp_path, mocker)
    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await pilot.pause()
        await _detection_finishes(app, pilot)

        option_list = app.screen.query_one("#model-catalog", OptionList)
        option_list.highlighted = 2
        await option_list.run_action("select")
        await pilot.pause()

        assert app.screen.query_one("#model", Input).value == "llama3.1:70b"


async def test_a_name_outside_the_catalogue_is_still_accepted(
    tmp_path: Path, mocker
) -> None:
    """§5: the list must never be a closed one. Ollama's library grows
    constantly, so a name that was never offered has to remain typeable —
    which is why this is an `Input` with a list beside it and not a `Select`."""
    from textual.widgets import Input

    app = _app(tmp_path, mocker)
    started = mocker.patch.object(app, "begin_install")

    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await pilot.pause()
        await _detection_finishes(app, pilot)
        screen = app.screen
        screen.query_one("#host", Input).value = "192.168.1.50"
        screen.query_one("#model", Input).value = "some-brand-new:14b"
        screen.query_one("#panel_password", Input).value = "contrasena-buena"
        screen.query_one("#panel_password_repeat", Input).value = "contrasena-buena"
        await _submit_and_confirm(pilot)

    assert started.call_args[0][0].ollama_model == "some-brand-new:14b"


async def test_an_empty_catalogue_leaves_a_usable_text_field(
    tmp_path: Path, mocker
) -> None:
    """Offline, or with Ollama unreachable, the catalogue can legitimately come
    back empty. The field must degrade to what it was before — a text box —
    rather than to an empty box that reads as "no models found"."""
    from textual.widgets import Input, OptionList

    app = _app(tmp_path, mocker)
    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await pilot.pause()
        await _detection_finishes(app, pilot, catalog=[])

        assert app.screen.query_one("#model-catalog", OptionList).display is False
        model_input = app.screen.query_one("#model", Input)
        model_input.value = "qwen2.5:1.5b"
        await pilot.pause()
        assert app.screen.query_one("#model", Input).value == "qwen2.5:1.5b"


async def test_the_recommendation_is_preselected(tmp_path: Path, mocker) -> None:
    from textual.widgets import Input

    app = _app(tmp_path, mocker)
    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await pilot.pause()
        await _detection_finishes(app, pilot, recommended=_catalog()[1])

        assert app.screen.query_one("#model", Input).value == "qwen2.5:3b"


async def test_the_model_already_in_env_wins_over_the_recommendation(
    tmp_path: Path, mocker
) -> None:
    """Someone who ran `axion-wizard model set` and then reinstalls must not
    lose that choice by simply pressing on — the same reason the CLI's prompt
    pre-selects it, and the same reason the PostgreSQL password is reused."""
    from textual.widgets import Input

    (tmp_path / ".env").write_text("OLLAMA_MODEL=llama3.1:70b\n", encoding="utf-8")

    app = _app(tmp_path, mocker)
    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await pilot.pause()
        await _detection_finishes(app, pilot, recommended=_catalog()[1])

        assert app.screen.query_one("#model", Input).value == "llama3.1:70b"


# --- the confirmation before anything is written ------------------------------------
#
# Step 3 of the CLI ends with a summary and one confirmation, because up to
# that point nothing has touched the disk. The TUI went straight from the form
# to the deployment: the interface that shows the most was the one that never
# showed what it was about to do.


async def _fill_valid_form(screen) -> None:
    from textual.widgets import Input

    screen.query_one("#host", Input).value = "192.168.1.50"
    screen.query_one("#model", Input).value = "qwen2.5:1.5b"
    screen.query_one("#panel_password", Input).value = "contrasena-buena"
    screen.query_one("#panel_password_repeat", Input).value = "contrasena-buena"


async def test_the_form_confirms_before_installing(tmp_path: Path, mocker) -> None:
    from axion_wizard.tui.app import ConfirmScreen

    app = _app(tmp_path, mocker)
    started = mocker.patch.object(app, "begin_install")

    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await pilot.pause()
        await _fill_valid_form(app.screen)
        await pilot.click("#start")
        await pilot.pause()

        assert isinstance(app.screen, ConfirmScreen)
        started.assert_not_called()


async def test_the_summary_shows_the_configuration_and_masks_the_secrets(
    tmp_path: Path, mocker
) -> None:
    """The same panel the CLI prints, so §9's masking comes with it rather
    than having to be re-implemented (and re-remembered) here."""
    from textual.widgets import Static

    app = _app(tmp_path, mocker)
    mocker.patch.object(app, "begin_install")

    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await pilot.pause()
        await _fill_valid_form(app.screen)
        await pilot.click("#start")
        await pilot.pause()

        summary = str(app.screen.query_one("#summary", Static).content)
        assert "192.168.1.50" in summary
        assert "qwen2.5:1.5b" in summary
        assert "contrasena-buena" not in summary


async def test_going_back_installs_nothing(tmp_path: Path, mocker) -> None:
    from axion_wizard.tui.app import ConfigScreen

    app = _app(tmp_path, mocker)
    started = mocker.patch.object(app, "begin_install")

    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await pilot.pause()
        await _fill_valid_form(app.screen)
        await pilot.click("#start")
        await pilot.pause()
        await pilot.click("#confirm-back")
        await pilot.pause()

        assert isinstance(app.screen, ConfigScreen)
        started.assert_not_called()


async def test_yes_skips_the_confirmation(tmp_path: Path, mocker) -> None:
    """`--yes` means "assume yes to everything"; asking once more here would
    be exactly the confirmation it was passed to avoid. The CLI bypasses step
    3's `questionary.confirm` the same way."""
    from axion_wizard.tui.app import AxionInstallerApp

    app = AxionInstallerApp(GlobalState(project_dir=tmp_path, yes=True))
    mocker.patch.object(app, "_detect_environment")
    app.detected_variant = WireguardVariant.PORTS.value
    app.environment_ready = True
    started = mocker.patch.object(app, "begin_install")

    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await pilot.pause()
        await _fill_valid_form(app.screen)
        await pilot.click("#start")
        await pilot.pause()

    started.assert_called_once()
