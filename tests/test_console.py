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
