"""Acceso a plantillas empaquetadas dentro del binario de PyInstaller (§7.2).

Nunca usar rutas relativas a `__file__`: dentro de un bundle `--onefile` esa
ruta apunta a un directorio temporal distinto en cada ejecución.
`importlib.resources` sí resuelve correctamente tanto en modo desarrollo
como empaquetado.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import as_file, files
from pathlib import Path

_TEMPLATES_PACKAGE = "axion_wizard.templates"


def read_template_text(relative_path: str) -> str:
    """Lee `axion_wizard/templates/<relative_path>` como texto UTF-8."""
    resource = files(_TEMPLATES_PACKAGE).joinpath(relative_path)
    return resource.read_text(encoding="utf-8")


def read_template_bytes(relative_path: str) -> bytes:
    resource = files(_TEMPLATES_PACKAGE).joinpath(relative_path)
    return resource.read_bytes()


@contextmanager
def template_filesystem_path(relative_path: str) -> Iterator[Path]:
    """Contextmanager que da una ruta de filesystem real a un recurso
    empaquetado, para herramientas (p.ej. `docker build`) que necesitan una
    ruta y no un objeto file-like."""
    resource = files(_TEMPLATES_PACKAGE).joinpath(relative_path)
    with as_file(resource) as path:
        yield path
