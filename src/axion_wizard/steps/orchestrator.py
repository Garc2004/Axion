"""Orchestration of the install flow (§4).

Runs the ten steps in order, persisting progress after each one so an
interrupted run resumes from the last completed step (§4, §12).

Resuming carries one nuance that shapes the whole design: the persisted state
records **only which steps finished**, never their values — the PostgreSQL
password and the panel credentials would travel there, and §9 does not allow
it. That is why skipping an already-done step is not simply not running it:
its `restore()` has to be called so it repopulates the context by reading the
artifacts it left on disk (`.env`, `wg.env`, `docker-compose.yml`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from rich.panel import Panel
from rich.table import Table

from axion_wizard.errors import AxionError
from axion_wizard.render import ui
from axion_wizard.render.console import console
from axion_wizard.steps.base import Step, StepResult
from axion_wizard.steps.context import InstallContext
from axion_wizard.utils import state as state_store

if TYPE_CHECKING:
    from axion_wizard.cli import GlobalState


def build_steps(state: GlobalState, context: InstallContext) -> list[Step]:
    """The ten steps, in §4's order.

    The imports live inside on purpose: each step module drags in its services
    (httpx, cryptography, questionary…), and loading them all when `cli` is
    imported would make even `--version` pay that startup cost.
    """
    from axion_wizard.steps.s01_environment import EnvironmentStep
    from axion_wizard.steps.s02_network import NetworkStep
    from axion_wizard.steps.s03_config import ConfigStep
    from axion_wizard.steps.s04_certificate import CertificateStep
    from axion_wizard.steps.s05_compose import ComposeStep
    from axion_wizard.steps.s06_deploy import DeployStep
    from axion_wizard.steps.s07_model import ModelStep
    from axion_wizard.steps.s08_wireguard import WireguardStep
    from axion_wizard.steps.s08b_bot_setup import BotSetupStep
    from axion_wizard.steps.s09_verify import VerifyStep

    step_types: tuple[type[Step], ...] = (
        EnvironmentStep,
        NetworkStep,
        ConfigStep,
        CertificateStep,
        ComposeStep,
        DeployStep,
        ModelStep,
        WireguardStep,
        BotSetupStep,
        VerifyStep,
    )
    return [step_type(state, context) for step_type in step_types]


def ordered_step_names(steps: list[Step]) -> list[str]:
    return [step.name for step in steps]


class StepReporter(Protocol):
    """How the user is told what is happening at each step.

    It exists so the TUI can use `run_steps` as-is. The TUI used to have its
    own step loop copied from here, and that copy was missing the one thing
    you cannot see by reading it: progress persistence. The result was that an
    interrupted `--tui` install did not resume — the README promises it does —
    and never called `restore()`.

    Separating *what is done* from *how it is narrated* is what lets both
    interfaces share the same loop without the logic knowing anything about
    Rich or Textual.
    """

    def on_step_resumed(self, index: int, total: int, step: Step) -> None: ...

    def on_step_invalidated(self, index: int, total: int, step: Step, reason: str) -> None: ...

    def on_step_skipped(self, index: int, total: int, step: Step, reason: str) -> None: ...

    def on_step_started(self, index: int, total: int, step: Step) -> None: ...

    def on_step_finished(self, index: int, total: int, step: Step, result: StepResult) -> None: ...

    def on_note(self, message: str) -> None: ...


class ConsoleStepReporter:
    """The default reporter: exactly the Rich output as it always was."""

    @staticmethod
    def _label(index: int, total: int, step: Step) -> str:
        return f"[{index}/{total}] {step.title or step.name}"

    def on_step_resumed(self, index: int, total: int, step: Step) -> None:
        label = self._label(index, total, step)
        console.print(f"[axion.dim]{ui.GLYPH_SKIPPED} {label} — already done, resuming[/]")

    def on_step_invalidated(self, index: int, total: int, step: Step, reason: str) -> None:
        label = self._label(index, total, step)
        console.print(
            f"[axion.warn]{ui.GLYPH_WARN} {label} — was recorded as done but no longer is "
            f"({reason}). This step and every one after it will be redone.[/]"
        )

    def on_step_skipped(self, index: int, total: int, step: Step, reason: str) -> None:
        label = self._label(index, total, step)
        console.print(f"[axion.dim]{ui.GLYPH_SKIPPED} {label} — skipped: {reason}[/]")

    def on_step_started(self, index: int, total: int, step: Step) -> None:
        console.print(f"\n[axion.title] {ui.GLYPH_RUNNING} {self._label(index, total, step)} [/]")

    def on_step_finished(self, index: int, total: int, step: Step, result: StepResult) -> None:
        if not result.ok:
            console.print(ui.fail(f"{step.title or step.name}: {result.message}"))

    def on_note(self, message: str) -> None:
        console.print(f"[axion.warn]{message}[/]")


def run_steps(
    state: GlobalState,
    context: InstallContext,
    steps: list[Step],
    reporter: StepReporter | None = None,
) -> bool:
    """Run the pending steps and return whether everything finished cleanly.

    Under `--dry-run` the state file is not touched: a simulation must not
    leave something marked as done that was not done, or the next real run
    would skip steps that were never applied.
    """
    reporter = reporter if reporter is not None else ConsoleStepReporter()
    persist = not state.dry_run
    wizard_state = (
        state_store.load_state(context.project_dir) if persist else state_store.WizardState()
    )

    total = len(steps)
    all_ok = True
    ordered_names = ordered_step_names(steps)

    for index, step in enumerate(steps, start=1):
        if wizard_state.is_complete(step.name):
            reporter.on_step_resumed(index, total, step)
            _restore_or_fail(step, wizard_state, context, persist, reporter, ordered_names)

            stale_reason = _stale_reason(step)
            if stale_reason is None:
                continue

            # The step is no longer genuinely applied. Everything that came
            # after was built on top of it, so it stops counting too:
            # `reset_from` discards from here to the end and the run falls
            # through to the normal path, redoing this step right now.
            reporter.on_step_invalidated(index, total, step, stale_reason)
            wizard_state.reset_from(step.name, ordered_names)
            if persist:
                state_store.save_state(context.project_dir, wizard_state)

        skip_reason = step.skip_reason()
        if skip_reason is not None:
            reporter.on_step_skipped(index, total, step, skip_reason)
            continue

        reporter.on_step_started(index, total, step)
        try:
            result = step.run()
        except AxionError as exc:
            if persist:
                wizard_state.mark_failed(step.name, str(exc))
                state_store.save_state(context.project_dir, wizard_state)
            raise

        reporter.on_step_finished(index, total, step, result)

        if not result.ok:
            all_ok = False
            if persist:
                wizard_state.mark_failed(step.name, result.message)
                state_store.save_state(context.project_dir, wizard_state)
            # Only the final step may fail without aborting: its job is to
            # report the state, and the table has already been printed.
            if step is not steps[-1]:
                break
            continue

        if persist:
            wizard_state.mark_complete(step.name, result.message)
            state_store.save_state(context.project_dir, wizard_state)

    return all_ok


def _stale_reason(step: Step) -> str | None:
    """Why a step marked as done no longer is, or `None` if it still holds.

    The state file says what happened last time, not what is true now.
    Without this check, a user who uninstalls Docker or deletes the containers
    finds that `install` skips the deployment — because "it was already done"
    — and goes straight to step 9 to fail every check, with no hint that the
    problem is seven steps earlier. That is exactly the case that motivated
    this: the state said "6 services operational" and not one was left.
    """
    if not step.revalidate_on_resume:
        return None
    try:
        result = step.verify()
    except AxionError as exc:
        return str(exc)
    if result.ok:
        return None
    return result.message or "this step's check did not pass"


def _restore_or_fail(
    step: Step,
    wizard_state: state_store.WizardState,
    context: InstallContext,
    persist: bool,
    reporter: StepReporter,
    ordered_names: list[str],
) -> None:
    """Repopulate the context of an already-completed step.

    If the artifacts it needed are gone — somebody deleted `.env`, or the
    project moved — the step stops counting as completed and runs again on the
    next pass, which beats carrying on with a half-built context and blowing
    up three steps later.

    And everything that came after it stops counting too, for the same reason
    as in `_stale_reason`: it was built on top of this step. Without that, the
    next run redid only this one and went on trusting the rest, which is how
    you end up with a state that says "6 services operational" when none
    exists.
    """
    try:
        step.restore()
    except AxionError as exc:
        reporter.on_note(
            f"Could not resume `{step.name}` ({exc}); this step and the ones after "
            "it will be redone on the next run."
        )
        wizard_state.reset_from(step.name, ordered_names)
        wizard_state.mark_failed(step.name, f"not resumable: {exc}")
        if persist:
            state_store.save_state(context.project_dir, wizard_state)
        raise


def render_resume_overview(steps: list[Step], wizard_state: state_store.WizardState) -> Panel:
    """The ten steps and where each one stands, before anything runs.

    This exists because of one specific and entirely reasonable confusion: on
    resume, the wizard printed eight grey "already done" lines and planted
    itself at step 9, without ever showing the whole picture or why it started
    there. With the map in front of you, "I am on 9" stops being a surprise
    and becomes information — and if that is not what the user wants, the
    panel's footer says how to start over.
    """
    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="right", style="axion.dim", width=4)
    grid.add_column(width=3)
    grid.add_column(overflow="fold")

    total = len(steps)
    pending_shown = False
    for index, step in enumerate(steps, start=1):
        record = next(
            (s for s in wizard_state.completed_steps if s.name == step.name), None
        )
        title = step.title or step.name

        if record is not None and record.ok:
            mark = f"[axion.ok]{ui.GLYPH_OK}[/]"
            detail = f"[axion.dim]{record.message}[/]" if record.message else ""
        elif record is not None:
            mark = f"[axion.error]{ui.GLYPH_FAIL}[/]"
            detail = f"[axion.error]{record.message}[/]" if record.message else ""
        elif not pending_shown:
            # The first undone step is where the run will begin.
            mark = f"[axion.info]{ui.GLYPH_RUNNING}[/]"
            detail = "[axion.info]starts here[/]"
            pending_shown = True
        else:
            mark = f"[axion.dim]{ui.GLYPH_PENDING}[/]"
            detail = ""

        grid.add_row(f"{index}/{total}", mark, f"{title}  {detail}".rstrip())

    grid.add_row("", "", "")
    grid.add_row(
        "",
        "",
        "[axion.dim]Ticked steps are checked before being taken as good; if they "
        "no longer hold, they are redone.\n"
        "To start from scratch: [/][axion.label]axion-wizard reset[/]",
    )

    return Panel(
        grid,
        title="[axion.heading]Saved progress from a previous run[/]",
        title_align="left",
        border_style="axion.border",
        padding=(1, 2),
    )


def render_closing_summary(context: InstallContext, all_ok: bool) -> Panel | None:
    """The closing panel: where to log in and what is still outstanding.

    The same visual language as `s03_config.render_summary` — the
    pre-deployment confirmation — on purpose: they are the two points in the
    flow where the user needs a complete picture of the state, one before
    anything is written and one after it finishes. Looking like the same
    family of panel is what makes the flow feel like one piece rather than
    loose text between tables.
    """
    config = context.config
    if config is None:
        return None

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="axion.label")
    grid.add_column(overflow="fold")
    grid.add_row("Mattermost", f"[axion.info]https://{config.host}[/]")
    grid.add_row(
        "WireGuard panel",
        f"[axion.info]http://{config.host}:51821[/]  [axion.dim](http, not https)[/]",
    )
    grid.add_row("AI model", config.ollama_model)

    if context.warnings:
        grid.add_row("", "")
        for warning in context.warnings:
            grid.add_row(ui.warn("Warning"), warning)

    grid.add_row("", "")
    grid.add_row("", "[axion.dim]Re-validate at any time with: axion-wizard doctor[/]")

    if all_ok:
        title = f"[axion.ok]{ui.GLYPH_OK} Install complete[/]"
    else:
        title = f"[axion.warn]{ui.GLYPH_WARN} Install finished with checks in red[/]"

    return Panel(
        grid,
        title=title,
        title_align="left",
        border_style="axion.ok" if all_ok else "axion.warn",
        padding=(1, 2),
    )


def summarize(context: InstallContext, all_ok: bool) -> None:
    console.print()
    panel = render_closing_summary(context, all_ok)
    if panel is not None:
        console.print(panel)


def install(state: GlobalState) -> bool:
    """Entry point of the complete flow. Returns whether all went well."""
    context = InstallContext(project_dir=state.project_dir)
    steps = build_steps(state, context)

    console.print(
        f"[axion.title] AXION Wizard — installing into {state.project_dir} [/]"
    )
    if state.dry_run:
        console.print(
            "[axion.info]--dry-run:[/] everything that would be done is shown, "
            "without touching the system."
        )

    # The full map before anything else: if there is saved progress, knowing
    # where it picks up and why is the first thing the user needs.
    previous = state_store.load_state(context.project_dir)
    if previous.completed_steps and not state.quiet:
        console.print()
        console.print(render_resume_overview(steps, previous))

    all_ok = run_steps(state, context, steps)
    summarize(context, all_ok)
    return all_ok
