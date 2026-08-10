"""Guardas para los pasos que preguntan algo.

Sin una terminal real, `questionary` no falla con algo que se pueda leer:
en Windows lanza `NoConsoleScreenBufferError("No Windows console found. Are
you running cmd.exe?")`, y en POSIX se queda esperando o devuelve `None` a
mitad del flujo. Ninguna de las dos cosas le dice al usuario qué hacer, y la
primera acababa saliendo por el manejador genérico como
`Error inesperado: ...`, que es exactamente lo que §8 prohíbe.
"""

from __future__ import annotations

import sys

from axion_wizard.errors import ConfigError


def interactive_input_available() -> bool:
    """`True` si hay una terminal al otro lado a la que preguntar.

    Se exigen **stdin y stdout** a la vez. Podría parecer que basta con
    stdin, porque lo que se quiere es leer una respuesta, pero questionary
    va sobre prompt_toolkit, que *dibuja* el prompt: en Windows necesita un
    búfer de pantalla de consola real y, si la salida está redirigida,
    revienta con `NoConsoleScreenBufferError` aunque stdin siga siendo una
    TTY. Comprobar solo stdin dejaba pasar `axion-wizard install > log.txt`
    y el flujo moría a mitad del paso 2.
    """
    for stream in (sys.stdin, sys.stdout):
        try:
            if stream is None or not stream.isatty():
                return False
        except (AttributeError, OSError, ValueError):
            return False
    return True


def require_interactive_input(what: str) -> None:
    """Lanza `ConfigError` accionable si no se puede preguntar nada."""
    if interactive_input_available():
        return
    raise ConfigError(
        what=f"{what} necesita una terminal interactiva",
        why=(
            "La entrada o la salida no son una terminal (tubería, redirección o CI), "
            "así que no se puede dibujar el prompt ni leer la respuesta."
        ),
        steps=[
            "Ejecutarlo directamente en una terminal, sin tuberías ni redirecciones.",
            "O sin prompts: axion-wizard install --unattended --config axion.toml",
        ],
    )
