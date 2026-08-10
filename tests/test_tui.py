"""Tests de la interfaz Textual (`install --tui`).

Se prueba con el `run_test()` de Textual, que arranca la app contra un
driver headless: sin él haría falta una terminal real y no habría forma de
cubrir esto en CI.
"""

from pathlib import Path

import pytest

from axion_wizard.cli import GlobalState
from axion_wizard.config import WireguardVariant
from axion_wizard.errors import ConfigError

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


#: Terminal holgada para los tests: con el tamaño por defecto, el botón
#: "Instalar" cae fuera de la región visible y `pilot.click` lo rechaza con
#: OutOfBounds — un artefacto del harness, no del formulario.
TERMINAL_SIZE = (120, 50)


def _app(tmp_path: Path, mocker, *, environment_ready: bool = True):
    """App con el paso 1 (detección) sustituido: arranca subprocess reales.

    `environment_ready` simula que esa detección ya terminó — es lo que el
    worker real hace al acabar, y sin ello el formulario mantiene el envío
    bloqueado a propósito (ver el botón `#start` de `ConfigScreen`).
    """
    from axion_wizard.tui.app import AxionInstallerApp

    app = AxionInstallerApp(GlobalState(project_dir=tmp_path))
    mocker.patch.object(app, "_detect_environment")
    app.detected_variant = WireguardVariant.PORTS.value
    app.environment_ready = environment_ready
    return app


# --- guardas de `--tui` -----------------------------------------------------------


def test_tui_rejects_unattended(tmp_path: Path) -> None:
    """Textual existe para rellenar un formulario a mano; sin nadie delante
    se quedaría esperando teclas, que desde fuera parece un cuelgue."""
    from axion_wizard.steps.runner import _assert_tui_is_usable

    with pytest.raises(ConfigError, match="unattended"):
        _assert_tui_is_usable(GlobalState(project_dir=tmp_path), unattended=True)


def test_tui_rejects_a_non_interactive_stdin(tmp_path: Path, mocker) -> None:
    from axion_wizard.steps.runner import _assert_tui_is_usable

    fake_stdin = mocker.Mock()
    fake_stdin.isatty.return_value = False
    mocker.patch("sys.stdin", fake_stdin)

    with pytest.raises(ConfigError, match="terminal interactiva"):
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

        assert "no coinciden" in screen.last_error


async def test_form_rejects_a_forbidden_character(tmp_path: Path, mocker) -> None:
    """Misma regla que el prompt de questionary: `$` se rechaza ANTES de
    hashear, con el motivo a la vista."""
    from textual.widgets import Input

    app = _app(tmp_path, mocker)
    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.query_one("#host", Input).value = "192.168.1.50"
        screen.query_one("#model", Input).value = "qwen2.5:1.5b"
        screen.query_one("#panel_password", Input).value = "tiene$dolar"
        screen.query_one("#panel_password_repeat", Input).value = "tiene$dolar"
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
    # La contraseña nunca se guarda en claro: solo su hash bcrypt (§9).
    assert config.wireguard_admin_password_hash.get_secret_value().startswith("$2b$")
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


# --- diseño: vocabulario compartido con la CLI y estructura del formulario -----------------


def test_app_uses_the_nord_theme(tmp_path: Path, mocker) -> None:
    """Coherencia visual: una paleta ya diseñada, no colores hex inventados
    a mano, y la misma para toda la app (no cambia entre pantallas)."""
    from axion_wizard.tui.app import APP_THEME

    app = _app(tmp_path, mocker)
    assert app.theme == APP_THEME


def test_step_status_glyphs_come_from_the_shared_ui_module(tmp_path: Path, mocker) -> None:
    """Que un ✓ en `doctor` (CLI) y un ✓ en `install --tui` sean el mismo
    carácter no es casualidad: ambos leen `axion_wizard.ui.GLYPH_*`."""
    from axion_wizard import ui
    from axion_wizard.tui.app import _STATUS_MARKS, DONE, FAILED, PENDING, RUNNING, SKIPPED

    assert _STATUS_MARKS[DONE][0] == ui.GLYPH_OK
    assert _STATUS_MARKS[FAILED][0] == ui.GLYPH_FAIL
    assert _STATUS_MARKS[PENDING][0] == ui.GLYPH_PENDING
    assert _STATUS_MARKS[RUNNING][0] == ui.GLYPH_RUNNING
    assert _STATUS_MARKS[SKIPPED][0] == ui.GLYPH_SKIPPED


async def test_form_is_grouped_into_titled_sections(tmp_path: Path, mocker) -> None:
    """Regresión de diseño: las tres secciones del formulario (Acceso,
    Seguridad, Modelo) deben seguir presentes y en orden — es lo que hace
    escaneable un formulario de cinco campos en vez de una lista plana."""
    from textual.widgets import Static

    app = _app(tmp_path, mocker)
    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await pilot.pause()
        rendered = [
            str(s.content) for s in app.screen.query(Static) if "section-title" in s.classes
        ]
        assert rendered == ["1 · Acceso", "2 · Seguridad", "3 · Modelo"]


async def test_error_banner_is_hidden_until_there_is_an_error(tmp_path: Path, mocker) -> None:
    """El banner de error es una caja bordeada, no una línea de texto suelta
    — pero solo cuando hay algo que mostrar: reservar el hueco siempre
    dejaría un vacío feo en un formulario ya apretado de espacio."""
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
    """Eco de lo que decidió el paso 1: la CLI lo muestra en una tabla
    completa, la TUI en una línea — pero no debe dejar al usuario a ciegas
    sobre qué detectó el wizard antes de rellenar el formulario."""
    from axion_wizard.config import WireguardVariant

    app = _app(tmp_path, mocker)
    app.detected_variant = WireguardVariant.HOST.value
    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await pilot.pause()
        summary = app.screen.query_one("#environment-summary")
        assert "host" in str(summary.content)


# --- pantalla de progreso ------------------------------------------------------------


async def test_progress_screen_lists_every_step(tmp_path: Path, mocker) -> None:
    from axion_wizard.tui.app import ProgressScreen, StepLine

    app = _app(tmp_path, mocker)
    titles = ["uno", "dos", "tres"]
    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await app.push_screen(ProgressScreen(titles))
        await pilot.pause()
        assert len(app.screen.query(StepLine)) == len(titles)


async def test_progress_screen_sections_have_titled_borders(tmp_path: Path, mocker) -> None:
    """`border_title` es una propiedad del widget, no texto del contenido:
    fácil de fijar sin querer sobre un widget que en el árbol real de la
    app no tiene CSS con `border:` que lo dibuje — verificado aquí sobre la
    app real (`AxionInstallerApp`), no una `App` genérica de prueba."""
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

        screen.set_step(0, DONE, "hecho")
        screen.set_step(1, FAILED, "reventó")
        await pilot.pause()

        lines = list(app.screen.query(StepLine))
        assert "hecho" in lines[0].text
        assert "reventó" in lines[1].text


# --- estado silenciado ---------------------------------------------------------------


def test_steps_run_quiet_and_unattended_under_the_tui(tmp_path: Path) -> None:
    """Los pasos imprimen con Rich sobre stdout; sin silenciarlos, sus tablas
    se cuelan por encima de la interfaz. Y ninguno debe abrir un prompt de
    questionary mientras Textual es dueño de la pantalla."""
    from axion_wizard.tui.app import _quiet_copy

    state = GlobalState(project_dir=tmp_path, verbose=True)
    quiet = _quiet_copy(state, unattended=True)

    assert quiet.quiet is True
    assert quiet.unattended is True
    assert quiet.project_dir == tmp_path
    assert state.quiet is False, "el original no debe mutarse"


# --- la contraseña de PostgreSQL no se regenera -------------------------------------
#
# Regresión: el formulario de la TUI generaba una contraseña nueva en cada
# corrida. Postgres solo aplica POSTGRES_PASSWORD al inicializar su volumen y
# la ignora después, así que sobre un proyecto ya desplegado Mattermost dejaba
# de autenticarse sin ningún error que lo explicara. El camino de la CLI ya lo
# evitaba (s03_config.existing_postgres_password documenta el incidente).


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


# --- no se puede enviar el formulario antes de detectar el entorno -------------------
#
# `on_mount` lanza la detección en un worker en hilo y muestra el formulario
# sin esperarla. Quien rellenara y pulsara antes de que terminara se llevaba el
# valor por defecto de la variante (`ports`) — en Linux nativo, la equivocada.


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


# --- paridad con la CLI: el progreso se persiste ------------------------------------
#
# El worker de la TUI tenía su propio bucle de pasos, copiado del orquestador
# pero sin la persistencia: una instalación `--tui` interrumpida no se
# reanudaba, pese a que el README lo promete para `install`.


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
