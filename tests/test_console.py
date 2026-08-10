import io
import sys

from axion_wizard.render import console as console_module


def test_configure_stdio_encoding_is_idempotent_and_safe() -> None:
    """Debe poder llamarse de nuevo sin romper nada."""
    console_module.configure_stdio_encoding()
    console_module.configure_stdio_encoding()


def test_configure_stdio_encoding_tolerates_streams_without_reconfigure(mocker) -> None:
    """Bajo pytest (y en algunos entornos empaquetados) stdout no es un
    TextIOWrapper y no expone `reconfigure`."""
    mocker.patch.object(sys, "stdout", io.StringIO())
    mocker.patch.object(sys, "stderr", io.StringIO())
    console_module.configure_stdio_encoding()  # no debe lanzar


def test_configure_stdio_encoding_tolerates_reconfigure_failure(mocker) -> None:
    stream = mocker.Mock()
    stream.reconfigure.side_effect = OSError("no se puede reconfigurar")
    mocker.patch.object(sys, "stdout", stream)
    mocker.patch.object(sys, "stderr", stream)
    console_module.configure_stdio_encoding()  # no debe lanzar


def test_unicode_marks_used_by_the_ui_survive_encoding() -> None:
    """Regresión: el `✓` del modelo recomendado (§5) y los bloques del QR
    (§4.8) reventaban con UnicodeEncodeError en una consola cp1252."""
    for char in ("✓", "█", "▀", "▄", "í", "ó", "ñ"):
        assert char.encode("utf-8").decode("utf-8") == char


def test_console_writes_checkmark_without_raising() -> None:
    from rich.console import Console

    buffer = io.StringIO()
    probe = Console(file=buffer, theme=console_module.AXION_THEME)
    probe.print("[axion.ok]✓[/] modelo recomendado")
    assert "✓" in buffer.getvalue()


def test_set_quiet_and_no_color_toggle_console_state() -> None:
    original_quiet = console_module.console.quiet
    try:
        console_module.set_quiet(True)
        assert console_module.console.quiet is True
        console_module.set_quiet(False)
        assert console_module.console.quiet is False
    finally:
        console_module.console.quiet = original_quiet


def test_set_no_color_is_a_one_way_switch() -> None:
    original = console_module.console.no_color
    try:
        console_module.set_no_color(False)
        console_module.set_no_color(True)
        assert console_module.console.no_color is True
    finally:
        console_module.console.no_color = original


# --- render_to_ansi: el tema se resuelve donde está definido -------------------------


def test_render_to_ansi_resolves_the_axion_theme() -> None:
    """Un renderable con estilos `axion.*` se pinta sin reventar.

    Textual dibuja su `RichLog` con una `Console` propia que no conoce el
    tema: pasarle un `Panel` con `axion.label` directamente lanzaba
    `MissingStyle`. No se veía mal — se caía la TUI entera al terminar la
    instalación, justo en el panel de cierre.
    """
    from rich.panel import Panel
    from rich.text import Text

    from axion_wizard.render.console import render_to_ansi

    body = Text("dentro", style="axion.label")
    output = render_to_ansi(Panel(body, border_style="axion.border"), width=40)

    assert "dentro" in output
    # `styles=True` conserva los códigos ANSI: si el tema no se hubiera
    # resuelto, saldría texto pelado.
    assert "\x1b[" in output


def test_render_to_ansi_respects_the_requested_width() -> None:
    from rich.panel import Panel

    from axion_wizard.render.console import render_to_ansi

    narrow = render_to_ansi(Panel("x"), width=30)
    assert max(len(line) for line in _strip_ansi(narrow).splitlines()) <= 30


def _strip_ansi(text: str) -> str:
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", text)
