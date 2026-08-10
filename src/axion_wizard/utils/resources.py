"""Access to templates packaged inside the PyInstaller binary (§7.2).

Never use paths relative to `__file__`: inside a `--onefile` bundle that path
points at a different temporary directory on every run. `importlib.resources`
resolves correctly both in development and when packaged.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import as_file, files
from pathlib import Path

_TEMPLATES_PACKAGE = "axion_wizard.templates"


def read_template_text(relative_path: str) -> str:
    """Read `axion_wizard/templates/<relative_path>` as UTF-8 text."""
    resource = files(_TEMPLATES_PACKAGE).joinpath(relative_path)
    return resource.read_text(encoding="utf-8")


def read_template_bytes(relative_path: str) -> bytes:
    resource = files(_TEMPLATES_PACKAGE).joinpath(relative_path)
    return resource.read_bytes()


@contextmanager
def template_filesystem_path(relative_path: str) -> Iterator[Path]:
    """Context manager giving a real filesystem path to a packaged resource,
    for tools (e.g. `docker build`) that need a path rather than a file-like
    object."""
    resource = files(_TEMPLATES_PACKAGE).joinpath(relative_path)
    with as_file(resource) as path:
        yield path
