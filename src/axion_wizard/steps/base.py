"""Clase base para los pasos del flujo de instalación."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from axion_wizard.cli import GlobalState
    from axion_wizard.steps.context import InstallContext


@dataclass
class StepResult:
    """Resultado de ejecutar un paso, persistido en el estado del wizard."""

    name: str
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    message: str = ""


class Step(ABC):
    """Un paso del flujo, ejecutable e idempotente.

    `run()` aplica el paso, `verify()` comprueba que quedó en el estado
    esperado (usado también por `doctor`), y `rollback()` deshace lo hecho
    si un paso posterior falla y el usuario decide abortar.
    """

    #: identificador estable, usado como clave en `.axion-wizard-state.json`
    name: str
    #: título legible, para la barra de progreso y el resumen
    title: str = ""
    #: Si al reanudar hay que comprobar con `verify()` que este paso *sigue*
    #: aplicado antes de darlo por bueno.
    #:
    #: El estado persistido dice lo que pasó la última vez, no lo que hay
    #: ahora: entre dos ejecuciones el usuario puede haber desinstalado
    #: Docker, borrado los contenedores o movido el proyecto. Sin
    #: revalidar, el wizard se salta pasos cuyo resultado ya no existe y
    #: falla mucho más adelante, culpando al paso equivocado.
    #:
    #: Se desactiva en los pasos que no producen nada de lo que dependan los
    #: siguientes: revalidarlos cuesta tiempo y no protege a nadie.
    revalidate_on_resume: bool = True

    def __init__(self, state: GlobalState, context: InstallContext) -> None:
        self.state = state
        self.context = context

    @abstractmethod
    def run(self) -> StepResult:
        """Ejecuta el paso. Debe ser idempotente."""

    @abstractmethod
    def verify(self) -> StepResult:
        """Comprueba que el paso quedó aplicado correctamente, sin modificar nada."""

    def restore(self) -> None:  # noqa: B027 - hook opcional, no todos los pasos restauran
        """Repuebla el contexto desde los artefactos ya escritos en disco.

        Es lo que hace posible reanudar sin volver a preguntar nada. El
        estado persistido guarda solo *qué* pasos terminaron, nunca sus
        valores (§9: ahí viajan contraseñas), así que al saltarse un paso ya
        completado hay que reconstruir lo que aportaba al contexto leyendo
        `.env`, `wg.env` o el propio `docker-compose.yml`.

        Por defecto no hace nada: los pasos que solo comprueban cosas —red,
        despliegue, verificación— no aportan nada que reconstruir.
        """

    def rollback(self) -> StepResult:
        """Deshace lo hecho por `run()`. No todos los pasos lo necesitan."""
        return StepResult(name=self.name, ok=True, message="sin rollback definido")

    def skip_reason(self) -> str | None:
        """Motivo por el que este paso no aplica en esta ejecución, o `None`.

        Permite que un paso se descarte por condiciones del entorno —no hay
        modelo que descargar, no toca crear cliente de WireGuard— sin que el
        orquestador tenga que conocer los detalles de cada uno.
        """
        return None
