"""Helpers shared by more than one command module."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from axion_wizard.errors import ConfigError
from axion_wizard.render.console import console

if TYPE_CHECKING:
    from axion_wizard.cli import GlobalState

CERT_RELATIVE_DIR = Path("nginx") / "certs"
COMPOSE_FILENAME = "docker-compose.yml"



def compose_path_of(state: GlobalState) -> Path:
    """Ruta del compose del proyecto, verificando que exista antes de que
    Docker falle con un mensaje mucho menos claro."""
    path = state.project_dir / COMPOSE_FILENAME
    if not path.exists():
        raise ConfigError(
            what=f"No se encontró {path}",
            why="Este subcomando opera sobre un stack ya generado por `axion-wizard install`.",
            steps=[
                "Ejecutar `axion-wizard install` primero.",
                "O pasar el directorio correcto con --project-dir.",
            ],
        )
    return path



def announce_dry_run(action: str) -> None:
    console.print(f"[axion.info][dry-run][/] {action}")
