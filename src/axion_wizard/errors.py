"""Jerarquía de excepciones del wizard.

Cada excepción lleva tres campos obligatorios: qué pasó (`what`), por qué
importa (`why`) y qué hacer (`steps`). El handler global de la CLI las
renderiza en un panel de Rich con ese formato; nunca se muestra un traceback
crudo al usuario salvo con `--verbose`.
"""

from __future__ import annotations


class AxionError(Exception):
    """Excepción base del wizard.

    Args:
        what: qué ocurrió, en una frase corta.
        why: por qué esto es un problema para el usuario.
        steps: lista de acciones concretas que el usuario puede tomar.
        title: título del panel de error (por defecto, el nombre de la clase).
    """

    title: str = "Error"

    def __init__(self, what: str, why: str, steps: list[str]) -> None:
        self.what = what
        self.why = why
        self.steps = steps
        super().__init__(what)

    def __str__(self) -> str:
        return self.what


class PlatformError(AxionError):
    """SO no soportado, Docker ausente, Compose v1, etc."""

    title = "Error de plataforma"


class NetworkError(AxionError):
    """CGNAT, puerto ocupado, sin conectividad."""

    title = "Error de red"


class ConfigError(AxionError):
    """Valor inválido, archivo corrupto."""

    title = "Error de configuración"


class DeploymentError(AxionError):
    """Fallo de compose, healthcheck agotado."""

    title = "Error de despliegue"


class OllamaError(AxionError):
    """Pull fallido, modelo inexistente."""

    title = "Error de Ollama"
