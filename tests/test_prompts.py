"""Guardas de interactividad de los pasos que preguntan (`steps/prompts.py`)."""

import io
import sys

import pytest

from axion_wizard.errors import ConfigError
from axion_wizard.steps.prompts import (
    interactive_input_available,
    require_interactive_input,
)


class _Stream(io.StringIO):
    def __init__(self, interactive: bool) -> None:
        super().__init__()
        self._interactive = interactive

    def isatty(self) -> bool:
        return self._interactive


def _streams(mocker, *, stdin: bool, stdout: bool) -> None:
    mocker.patch.object(sys, "stdin", _Stream(stdin))
    mocker.patch.object(sys, "stdout", _Stream(stdout))


def test_available_when_both_streams_are_a_terminal(mocker) -> None:
    _streams(mocker, stdin=True, stdout=True)
    assert interactive_input_available() is True


def test_unavailable_when_output_is_redirected(mocker) -> None:
    """Aunque stdin siga siendo TTY: prompt_toolkit *dibuja* el prompt y en
    Windows necesita un búfer de consola real, o lanza
    NoConsoleScreenBufferError a mitad del flujo."""
    _streams(mocker, stdin=True, stdout=False)
    assert interactive_input_available() is False


def test_unavailable_when_input_is_redirected(mocker) -> None:
    _streams(mocker, stdin=False, stdout=True)
    assert interactive_input_available() is False


def test_unavailable_when_a_stream_is_none(mocker) -> None:
    """En un bundle sin consola, `sys.stdout` puede ser None."""
    mocker.patch.object(sys, "stdin", _Stream(True))
    mocker.patch.object(sys, "stdout", None)
    assert interactive_input_available() is False


def test_unavailable_when_isatty_raises(mocker) -> None:
    """Un stream ya cerrado: no se puede afirmar que haya terminal, y lanzar
    desde aquí convertiría una salida limpia en un fallo."""

    class _Broken(io.StringIO):
        def isatty(self) -> bool:
            raise ValueError("I/O operation on closed file")

    mocker.patch.object(sys, "stdin", _Broken())
    mocker.patch.object(sys, "stdout", _Stream(True))
    assert interactive_input_available() is False


def test_require_passes_through_when_interactive(mocker) -> None:
    _streams(mocker, stdin=True, stdout=True)
    require_interactive_input("Algo")  # no debe lanzar


def test_require_raises_an_actionable_error(mocker) -> None:
    _streams(mocker, stdin=False, stdout=False)
    with pytest.raises(ConfigError) as excinfo:
        require_interactive_input("La configuración interactiva")

    error = excinfo.value
    assert "interactive terminal" in error.what
    assert any("--unattended" in step for step in error.steps)
