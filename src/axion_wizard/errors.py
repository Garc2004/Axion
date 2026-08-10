"""The wizard's exception hierarchy.

Every exception carries three mandatory fields: what happened (`what`), why
it matters (`why`) and what to do about it (`steps`). The CLI's global
handler renders them into a Rich panel in that shape; a raw traceback is
never shown to the user except under `--verbose`.
"""

from __future__ import annotations


class AxionError(Exception):
    """Base exception of the wizard.

    Args:
        what: what happened, in one short sentence.
        why: why this is a problem for the user.
        steps: concrete actions the user can take.
        title: error panel title (defaults to the class's own).
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
    """Unsupported OS, Docker missing, Compose v1, and the like."""

    title = "Platform error"


class NetworkError(AxionError):
    """CGNAT, busy port, no connectivity."""

    title = "Network error"


class ConfigError(AxionError):
    """Invalid value, corrupt file."""

    title = "Configuration error"


class DeploymentError(AxionError):
    """Compose failure, healthcheck timed out."""

    title = "Deployment error"


class OllamaError(AxionError):
    """Failed pull, nonexistent model."""

    title = "Ollama error"
