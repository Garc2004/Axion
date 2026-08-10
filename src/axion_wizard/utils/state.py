"""Persistencia del progreso del wizard, para reanudar una instalación
interrumpida desde el último paso completado.

Se guarda en `.axion-wizard-state.json` dentro del `project_dir` (§4,
arquitectura). Deliberadamente no persiste secretos: solo el nombre de cada
paso, si terminó bien, y un mensaje corto — los valores reales (contraseñas,
hashes) viven en `.env`/`wg.env`, no aquí.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

STATE_FILENAME = ".axion-wizard-state.json"
STATE_SCHEMA_VERSION = 1


@dataclass
class StepRecord:
    name: str
    ok: bool
    message: str = ""


@dataclass
class WizardState:
    schema_version: int = STATE_SCHEMA_VERSION
    completed_steps: list[StepRecord] = field(default_factory=list)

    def is_complete(self, step_name: str) -> bool:
        return any(s.name == step_name and s.ok for s in self.completed_steps)

    def mark_complete(self, step_name: str, message: str = "") -> None:
        self._drop(step_name)
        self.completed_steps.append(StepRecord(name=step_name, ok=True, message=message))

    def mark_failed(self, step_name: str, message: str = "") -> None:
        self._drop(step_name)
        self.completed_steps.append(StepRecord(name=step_name, ok=False, message=message))

    def _drop(self, step_name: str) -> None:
        self.completed_steps = [s for s in self.completed_steps if s.name != step_name]

    def reset_from(self, step_name: str, ordered_step_names: list[str]) -> None:
        """Descarta el estado de `step_name` en adelante — para cuando el
        usuario fuerza re-ejecutar un paso ya completado y todo lo que
        depende de él debe considerarse potencialmente inválido también."""
        if step_name not in ordered_step_names:
            return
        to_drop = set(ordered_step_names[ordered_step_names.index(step_name) :])
        self.completed_steps = [s for s in self.completed_steps if s.name not in to_drop]


def state_path(project_dir: Path) -> Path:
    return project_dir / STATE_FILENAME


def load_state(project_dir: Path) -> WizardState:
    """Nunca lanza: un archivo de estado ausente, corrupto, o de un schema
    desconocido se trata como "sin progreso previo" en vez de bloquear al
    usuario — el estado es una optimización, no una fuente de verdad crítica."""
    path = state_path(project_dir)
    if not path.exists():
        return WizardState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return WizardState()

    if not isinstance(raw, dict) or raw.get("schema_version") != STATE_SCHEMA_VERSION:
        return WizardState()

    try:
        steps = [StepRecord(**s) for s in raw.get("completed_steps", [])]
    except TypeError:
        return WizardState()
    return WizardState(schema_version=raw["schema_version"], completed_steps=steps)


def save_state(project_dir: Path, state: WizardState) -> None:
    """Persiste el progreso de forma atómica.

    Se escribe primero a un temporal en el mismo directorio y luego se hace
    `os.replace`, que es atómico. Escribir directamente sobre el archivo
    definitivo dejaría una ventana en la que una interrupción —justo el
    escenario para el que existe este archivo— lo dejaría truncado, y el
    wizard perdería todo el progreso al reanudar.
    """
    project_dir.mkdir(parents=True, exist_ok=True)
    path = state_path(project_dir)
    payload = json.dumps(asdict(state), indent=2, ensure_ascii=False) + "\n"

    # El temporal debe vivir en el mismo directorio: `os.replace` solo es
    # atómico dentro de un mismo sistema de archivos.
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=project_dir,
        prefix=f"{STATE_FILENAME}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        temp_path = Path(handle.name)

    try:
        os.replace(temp_path, path)
    except OSError:
        # Sin esto, cada intento fallido —un antivirus con el archivo abierto,
        # el disco lleno— dejaba un `.axion-wizard-state.json.*.tmp` suelto en
        # el directorio del proyecto, y el usuario no tiene forma de saber que
        # son basura nuestra.
        temp_path.unlink(missing_ok=True)
        raise


def next_pending_step(ordered_step_names: list[str], state: WizardState) -> str | None:
    """El primer paso, en orden, que no está marcado como completado
    exitosamente — el punto desde el que `install` reanuda."""
    for name in ordered_step_names:
        if not state.is_complete(name):
            return name
    return None
