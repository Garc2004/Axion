"""Orquestación del flujo de instalación (§4).

Ejecuta los diez pasos en orden, persistiendo el progreso tras cada uno
para que una ejecución interrumpida se reanude desde el último completado
(§4, §12).

La reanudación tiene un matiz que condiciona todo el diseño: el estado
persistido guarda **solo qué pasos terminaron**, nunca sus valores — ahí
viajarían la contraseña de PostgreSQL y el hash del panel, y §9 no lo
permite. Por eso saltarse un paso ya hecho no es simplemente no ejecutarlo:
hay que llamar a su `restore()` para que repueble el contexto leyendo los
artefactos que dejó en disco (`.env`, `wg.env`, `docker-compose.yml`).
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
    """Los diez pasos, en el orden de §4.

    Los imports van dentro a propósito: cada módulo de paso arrastra sus
    servicios (httpx, cryptography, questionary…), y cargarlos todos al
    importar `cli` haría que hasta `--version` pagase ese arranque.
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
    """Cómo se le cuenta al usuario lo que va pasando en cada paso.

    Existe para que la TUI pueda usar `run_steps` tal cual. Antes tenía su
    propio bucle de pasos copiado de aquí, y esa copia se quedó sin lo único
    que no se ve al leerla: la persistencia del progreso. Resultado, una
    instalación `--tui` interrumpida no se reanudaba —el README promete que
    sí— y nunca llamaba a `restore()`.

    Separar *qué se hace* de *cómo se cuenta* es lo que permite que las dos
    interfaces compartan el mismo bucle sin que la lógica sepa nada de Rich
    ni de Textual.
    """

    def on_step_resumed(self, index: int, total: int, step: Step) -> None: ...

    def on_step_invalidated(self, index: int, total: int, step: Step, reason: str) -> None: ...

    def on_step_skipped(self, index: int, total: int, step: Step, reason: str) -> None: ...

    def on_step_started(self, index: int, total: int, step: Step) -> None: ...

    def on_step_finished(self, index: int, total: int, step: Step, result: StepResult) -> None: ...

    def on_note(self, message: str) -> None: ...


class ConsoleStepReporter:
    """El reporter por defecto: exactamente la salida Rich de siempre."""

    @staticmethod
    def _label(index: int, total: int, step: Step) -> str:
        return f"[{index}/{total}] {step.title or step.name}"

    def on_step_resumed(self, index: int, total: int, step: Step) -> None:
        label = self._label(index, total, step)
        console.print(f"[axion.dim]{ui.GLYPH_SKIPPED} {label} — ya completado, se reanuda[/]")

    def on_step_invalidated(self, index: int, total: int, step: Step, reason: str) -> None:
        label = self._label(index, total, step)
        console.print(
            f"[axion.warn]{ui.GLYPH_WARN} {label} — figuraba como hecho pero ya no lo está "
            f"({reason}). Se rehace este paso y todos los siguientes.[/]"
        )

    def on_step_skipped(self, index: int, total: int, step: Step, reason: str) -> None:
        label = self._label(index, total, step)
        console.print(f"[axion.dim]{ui.GLYPH_SKIPPED} {label} — omitido: {reason}[/]")

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
    """Ejecuta los pasos pendientes y devuelve si todo terminó bien.

    Con `--dry-run` no se toca el archivo de estado: una simulación no debe
    dejar marcado como hecho algo que no se hizo, o la siguiente ejecución
    real se saltaría pasos que nunca llegaron a aplicarse.
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

            # El paso ya no está aplicado de verdad. Todo lo que venía
            # detrás se construyó sobre él, así que también deja de contar:
            # `reset_from` descarta desde aquí hasta el final y la ejecución
            # cae al camino normal, rehaciendo este paso ahora mismo.
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
            # Solo el paso final puede fallar sin abortar: su cometido es
            # informar del estado, y la tabla ya se imprimió.
            if step is not steps[-1]:
                break
            continue

        if persist:
            wizard_state.mark_complete(step.name, result.message)
            state_store.save_state(context.project_dir, wizard_state)

    return all_ok


def _stale_reason(step: Step) -> str | None:
    """Por qué un paso marcado como hecho ya no lo está, o `None` si sigue bien.

    El archivo de estado cuenta lo que pasó la última vez, no lo que hay
    ahora. Sin esta comprobación, un usuario que desinstala Docker o borra
    los contenedores se encuentra con que `install` se salta el despliegue
    —porque "ya se hizo"— y va directo al paso 9 a fallar todas las
    verificaciones, sin ninguna pista de que el problema está siete pasos
    antes. Es exactamente el caso que motivó esto: el estado decía
    "6 servicios operativos" y no quedaba ni uno.
    """
    if not step.revalidate_on_resume:
        return None
    try:
        result = step.verify()
    except AxionError as exc:
        return str(exc)
    if result.ok:
        return None
    return result.message or "la comprobación de este paso no pasó"


def _restore_or_fail(
    step: Step,
    wizard_state: state_store.WizardState,
    context: InstallContext,
    persist: bool,
    reporter: StepReporter,
    ordered_names: list[str],
) -> None:
    """Repuebla el contexto de un paso ya completado.

    Si los artefactos que necesitaba ya no están —alguien borró `.env`, o el
    proyecto se movió—, el paso deja de contar como completado y se vuelve a
    ejecutar en la siguiente pasada, que es preferible a seguir con un
    contexto a medias y reventar tres pasos más allá.

    Y con él deja de contar todo lo que venía detrás, por el mismo motivo
    que en `_stale_reason`: se construyó sobre este paso. Sin eso, la
    siguiente ejecución rehacía solo este y volvía a dar por buenos los
    demás, que es cómo se llega a un estado que dice "6 servicios
    operativos" sin que exista ninguno.
    """
    try:
        step.restore()
    except AxionError as exc:
        reporter.on_note(
            f"No se pudo reanudar `{step.name}` ({exc}); se rehará este paso y los "
            "siguientes en la próxima ejecución."
        )
        wizard_state.reset_from(step.name, ordered_names)
        wizard_state.mark_failed(step.name, f"no reanudable: {exc}")
        if persist:
            state_store.save_state(context.project_dir, wizard_state)
        raise


def render_resume_overview(steps: list[Step], wizard_state: state_store.WizardState) -> Panel:
    """Los diez pasos y en cuál está cada uno, antes de ejecutar nada.

    Existe por una confusión concreta y razonable: al reanudar, el wizard
    imprimía ocho líneas grises de "ya completado" y se plantaba en el paso
    9, sin que en ningún momento se viera el conjunto ni por qué empezaba
    ahí. Con el mapa delante, "voy por el 9" deja de ser una sorpresa y se
    convierte en información — y si no es lo que el usuario quiere, el pie
    del panel le dice cómo empezar de cero.
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
            # El primero sin hacer es donde va a arrancar la ejecución.
            mark = f"[axion.info]{ui.GLYPH_RUNNING}[/]"
            detail = "[axion.info]empieza aquí[/]"
            pending_shown = True
        else:
            mark = f"[axion.dim]{ui.GLYPH_PENDING}[/]"
            detail = ""

        grid.add_row(f"{index}/{total}", mark, f"{title}  {detail}".rstrip())

    grid.add_row("", "", "")
    grid.add_row(
        "",
        "",
        "[axion.dim]Los pasos marcados se comprueban antes de darlos por buenos; "
        "si ya no se sostienen, se rehacen.\n"
        "Para empezar de cero: [/][axion.label]axion-wizard reset[/]",
    )

    return Panel(
        grid,
        title="[axion.heading]Progreso guardado de una ejecución anterior[/]",
        title_align="left",
        border_style="axion.border",
        padding=(1, 2),
    )


def render_closing_summary(context: InstallContext, all_ok: bool) -> Panel | None:
    """Panel de cierre: dónde entrar y qué quedó pendiente.

    Mismo lenguaje visual que `s03_config.render_summary` —la confirmación
    previa al despliegue— a propósito: son los dos puntos del flujo donde el
    usuario necesita una foto completa del estado, uno antes de escribir
    nada y otro después de terminar. Que se vean como la misma familia de
    panel es lo que hace que el flujo se sienta como una sola pieza y no
    como texto suelto entre tablas.
    """
    config = context.config
    if config is None:
        return None

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="axion.label")
    grid.add_column(overflow="fold")
    grid.add_row("Mattermost", f"[axion.info]https://{config.host}[/]")
    grid.add_row(
        "Panel WireGuard",
        f"[axion.info]http://{config.host}:51821[/]  [axion.dim](http, no https)[/]",
    )
    grid.add_row("Modelo de IA", config.ollama_model)

    if context.warnings:
        grid.add_row("", "")
        for warning in context.warnings:
            grid.add_row(ui.warn("Aviso"), warning)

    grid.add_row("", "")
    grid.add_row("", "[axion.dim]Re-validar en cualquier momento con: axion-wizard doctor[/]")

    if all_ok:
        title = f"[axion.ok]{ui.GLYPH_OK} Instalación completada[/]"
    else:
        title = f"[axion.warn]{ui.GLYPH_WARN} Instalación terminada con comprobaciones en rojo[/]"

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
    """Punto de entrada del flujo completo. Devuelve si terminó todo bien."""
    context = InstallContext(project_dir=state.project_dir)
    steps = build_steps(state, context)

    console.print(
        f"[axion.title] AXION Wizard — instalación en {state.project_dir} [/]"
    )
    if state.dry_run:
        console.print(
            "[axion.info]--dry-run:[/] se mostrará todo lo que se haría, sin tocar el sistema."
        )

    # El mapa completo antes de nada: si hay progreso guardado, saber en qué
    # punto se retoma y por qué es lo primero que el usuario necesita.
    previous = state_store.load_state(context.project_dir)
    if previous.completed_steps and not state.quiet:
        console.print()
        console.print(render_resume_overview(steps, previous))

    all_ok = run_steps(state, context, steps)
    summarize(context, all_ok)
    return all_ok
