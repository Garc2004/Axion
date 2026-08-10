"""Vocabulario visual compartido entre la salida Rich (CLI) y la TUI Textual.

Antes de este módulo, cada tabla del wizard —`network-check`, `models`,
`doctor`, el entorno detectado, la pantalla de progreso de la TUI— definía
sus propios colores y símbolos de estado por su cuenta. El resultado eran
cinco superficies que no se sentían como la misma aplicación: unas decían
"OK"/"FALLO" en texto plano, otras usaban glifos (`✓`/`✕`) solo en la TUI, y
los bordes de panel mezclaban nombres de color sueltos (`"cyan"`, `"red"`)
en vez de los tokens semánticos ya definidos en `console.py`.

Aquí vive la única definición de "qué aspecto tiene un OK" y "qué aspecto
tiene una tabla de reporte del wizard", para que cualquier pantalla nueva
la reutilice en vez de reinventarla.
"""

from __future__ import annotations

from rich import box
from rich.table import Table

# --- Glifos de estado ---------------------------------------------------------------
#
# Los mismos seis estados que usa la pantalla de progreso de la TUI
# (`tui/app.py`): que un ✓ signifique lo mismo en una tabla de `doctor` que
# en la lista de pasos de `install --tui` es lo que hace que las dos
# interfaces se sientan como una sola aplicación y no como dos productos
# distintos que comparten nombre.
GLYPH_OK = "✓"
GLYPH_FAIL = "✕"
GLYPH_WARN = "▲"
GLYPH_PENDING = "○"
GLYPH_RUNNING = "◐"
GLYPH_SKIPPED = "−"

#: Glifo + estilo Rich por estado, para quien necesite ambos por separado
#: (la TUI, que traduce esto a color de Textual en vez de markup Rich).
STATUS_STYLE: dict[str, tuple[str, str]] = {
    "ok": (GLYPH_OK, "axion.ok"),
    "fail": (GLYPH_FAIL, "axion.error"),
    "warn": (GLYPH_WARN, "axion.warn"),
    "pending": (GLYPH_PENDING, "axion.dim"),
    "running": (GLYPH_RUNNING, "axion.info"),
    "skipped": (GLYPH_SKIPPED, "axion.dim"),
}


def ok(label: str = "OK") -> str:
    """Marca de estado positivo lista para una celda de tabla Rich."""
    return f"[axion.ok]{GLYPH_OK} {label}[/]"


def fail(label: str = "FALLO") -> str:
    return f"[axion.error]{GLYPH_FAIL} {label}[/]"


def warn(label: str) -> str:
    return f"[axion.warn]{GLYPH_WARN} {label}[/]"


def status(passed: bool, ok_label: str = "OK", fail_label: str = "FALLO") -> str:
    """Atajo para el caso más común de todas las tablas del wizard: una
    columna booleana OK/FALLO."""
    return ok(ok_label) if passed else fail(fail_label)


# --- Tablas y paneles -----------------------------------------------------------------


def make_table(title: str) -> Table:
    """Tabla con el aspecto común a todos los reportes del wizard.

    Centralizarlo aquí es lo que evita que `network-check`, `models`,
    `doctor` y el entorno detectado (§4.1) se vean como cuatro tablas de
    cuatro librerías distintas: mismo box, mismo estilo de cabecera, mismo
    color de título y de borde.
    """
    return Table(
        title=title,
        title_style="axion.heading",
        # Un nombre de estilo del tema, no compuesto ("bold axion.accent"):
        # Rich solo resuelve contra el tema un nombre de estilo *solo* — una
        # cadena compuesta intenta parsear cada palabra como color literal y
        # revienta con `MissingStyle` en cuanto no reconoce "axion.accent"
        # como color válido.
        header_style="axion.heading",
        box=box.ROUNDED,
        border_style="axion.border",
        pad_edge=False,
    )
