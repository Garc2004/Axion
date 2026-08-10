"""Instancia Rich compartida y tema de colores del wizard."""

from __future__ import annotations

import ctypes
import io
import sys

from rich.console import Console, RenderableType
from rich.theme import Theme

#: Codepage UTF-8 de Windows, para `SetConsoleOutputCP`.
_WINDOWS_UTF8_CODEPAGE = 65001


def configure_stdio_encoding() -> None:
    """Fuerza UTF-8 en stdout/stderr antes de imprimir nada.

    La consola de Windows usa por defecto una codepage regional (cp1252 en
    Europa occidental) que no puede representar buena parte de lo que este
    wizard imprime: el `✓` del modelo recomendado (§5), los bloques Unicode
    del QR de WireGuard (§4.8) y hasta los acentos de sus propios mensajes.
    Sin esto, `models` y `wireguard add-client` no es que se vean mal — se
    caen con `UnicodeEncodeError` a mitad de la salida.

    Se hacen dos cosas distintas y complementarias: `reconfigure` evita el
    error al codificar, y `SetConsoleOutputCP` hace que la consola además
    los dibuje bien. `errors="replace"` deja el fallo como un carácter feo
    en el peor caso, nunca como una excepción.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue  # p.ej. stdout capturado por pytest
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

    if sys.platform == "win32":
        try:
            ctypes.windll.kernel32.SetConsoleOutputCP(_WINDOWS_UTF8_CODEPAGE)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass


configure_stdio_encoding()

AXION_THEME = Theme(
    {
        # --- Semántica de estado: mismo significado en cualquier pantalla ---------
        "axion.ok": "bold green",
        "axion.warn": "bold yellow",
        "axion.error": "bold red",
        "axion.info": "bold cyan",
        # --- Estructura: bordes, cabeceras y títulos, no estados -------------------
        #
        # Separado de "info" a propósito: "info" es para texto que el usuario
        # debe leer como dato (p.ej. una URL); "accent" es para el andamiaje
        # visual (bordes de tabla, cabeceras) que debe notarse sin competir
        # por atención con el contenido.
        "axion.accent": "cyan",
        "axion.border": "cyan",
        "axion.heading": "bold cyan",
        "axion.label": "bold",
        # --- Texto secundario y secretos --------------------------------------------
        "axion.dim": "grey62",
        "axion.secret": "grey42",
        # --- Banner de sección: bloque con fondo, para un título por pantalla ------
        "axion.title": "bold white on blue",
    }
)

console = Console(theme=AXION_THEME)
error_console = Console(theme=AXION_THEME, stderr=True)


def set_quiet(quiet: bool) -> None:
    console.quiet = quiet


def set_no_color(no_color: bool) -> None:
    if no_color:
        console.no_color = True
        error_console.no_color = True


def render_to_ansi(renderable: RenderableType, width: int = 100) -> str:
    """Pinta un renderable con el tema AXION y devuelve el texto con ANSI.

    Existe por la TUI. Textual dibuja su `RichLog` con una `Console` propia,
    que no conoce el tema `axion.*`: escribir ahí un `Panel` que use
    `axion.label` revienta con `MissingStyle` — no se ve mal, se cae.

    Resolviendo el tema aquí, donde está definido, la TUI solo tiene que
    reproducir los colores ya resueltos. Es lo que permite que las dos
    interfaces compartan un mismo renderizador (`render_closing_summary`)
    en vez de reescribir su contenido a mano en cada una.
    """
    buffer = Console(
        theme=AXION_THEME,
        width=width,
        file=io.StringIO(),
        record=True,
        force_terminal=True,
    )
    buffer.print(renderable)
    return buffer.export_text(styles=True)
