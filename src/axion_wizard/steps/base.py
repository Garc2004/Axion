"""Base class for the steps of the install flow."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from axion_wizard.cli import GlobalState
    from axion_wizard.steps.context import InstallContext


@dataclass
class StepResult:
    """The result of running a step, persisted into the wizard's state."""

    name: str
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    message: str = ""


class Step(ABC):
    """One step of the flow, runnable and idempotent.

    `run()` applies the step, `verify()` checks it ended in the expected state
    (also used by `doctor`), and `rollback()` undoes what was done if a later
    step fails and the user chooses to abort.
    """

    #: Stable identifier, used as the key in `.axion-wizard-state.json`.
    name: str
    #: Human-readable title, for the progress bar and the summary.
    title: str = ""
    #: Whether resuming should confirm with `verify()` that this step is
    #: *still* applied before taking it as done.
    #:
    #: The persisted state says what happened last time, not what is true now:
    #: between two runs the user may have uninstalled Docker, deleted the
    #: containers, or moved the project. Without revalidating, the wizard
    #: skips steps whose result no longer exists and fails much later,
    #: blaming the wrong step.
    #:
    #: Turned off for steps that produce nothing the later ones depend on:
    #: revalidating those costs time and protects nobody.
    revalidate_on_resume: bool = True

    def __init__(self, state: GlobalState, context: InstallContext) -> None:
        self.state = state
        self.context = context

    @abstractmethod
    def run(self) -> StepResult:
        """Run the step. Must be idempotent."""

    @abstractmethod
    def verify(self) -> StepResult:
        """Check the step was applied correctly, without modifying anything."""

    def restore(self) -> None:  # noqa: B027 - optional hook; not every step restores
        """Repopulate the context from the artifacts already written to disk.

        This is what makes resuming possible without asking anything again.
        The persisted state records only *which* steps finished, never their
        values (§9: passwords travel there), so skipping an already-completed
        step means rebuilding what it contributed to the context by reading
        `.env`, `wg.env` or `docker-compose.yml` itself.

        Does nothing by default: the steps that only check things — network,
        deployment, verification — contribute nothing to rebuild.
        """

    def rollback(self) -> StepResult:
        """Undo what `run()` did. Not every step needs one."""
        return StepResult(name=self.name, ok=True, message="no rollback defined")

    def skip_reason(self) -> str | None:
        """Why this step does not apply on this run, or `None`.

        Lets a step opt out on environmental grounds — no model to download,
        no WireGuard client to create — without the orchestrator having to
        know the details of any of them.
        """
        return None
