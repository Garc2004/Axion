"""Persisting the wizard's progress, so an interrupted install resumes from
the last completed step.

Stored in `.axion-wizard-state.json` inside the `project_dir` (§4,
architecture). It deliberately persists no secrets: only each step's name,
whether it finished cleanly, and a short message — the real values
(passwords, tokens) live in `.env`/`wg.env`, not here.
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
        """Discard the state from `step_name` onwards — for when a completed
        step is re-run and everything that was built on top of it has to be
        treated as potentially invalid too."""
        if step_name not in ordered_step_names:
            return
        to_drop = set(ordered_step_names[ordered_step_names.index(step_name) :])
        self.completed_steps = [s for s in self.completed_steps if s.name not in to_drop]


def state_path(project_dir: Path) -> Path:
    return project_dir / STATE_FILENAME


def load_state(project_dir: Path) -> WizardState:
    """Never raises: a missing, corrupt, or unknown-schema state file is
    treated as "no previous progress" rather than blocking the user — this
    state is an optimisation, not a critical source of truth."""
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
    """Persist the progress atomically.

    Written first to a temporary file in the same directory, then moved with
    `os.replace`, which is atomic. Writing straight over the final file would
    leave a window in which an interruption — precisely the scenario this file
    exists for — would truncate it, and the wizard would lose all its progress
    on resume.
    """
    project_dir.mkdir(parents=True, exist_ok=True)
    path = state_path(project_dir)
    payload = json.dumps(asdict(state), indent=2, ensure_ascii=False) + "\n"

    # The temporary file has to live in the same directory: `os.replace` is
    # only atomic within a single filesystem.
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
        # Without this, every failed attempt — antivirus holding the file
        # open, a full disk — left a stray `.axion-wizard-state.json.*.tmp` in
        # the project directory, and the user has no way to know it is our
        # litter.
        temp_path.unlink(missing_ok=True)
        raise


def next_pending_step(ordered_step_names: list[str], state: WizardState) -> str | None:
    """The first step, in order, not marked as successfully completed — the
    point `install` resumes from."""
    for name in ordered_step_names:
        if not state.is_complete(name):
            return name
    return None
