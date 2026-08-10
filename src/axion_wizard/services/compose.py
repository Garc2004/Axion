"""Wrapper de `docker compose` por subprocess (§4.6, §1.3).

Invocar `docker compose` vía subprocess es más fiable y transparente que el
SDK docker-py, cuyo soporte de Compose v2 es pobre — de ahí que este módulo
sea una fina capa sobre `utils.shell`, sin dependencias de `docker` (paquete).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from axion_wizard.errors import ConfigError, DeploymentError
from axion_wizard.utils.jsonio import parse_json_lines_or_array
from axion_wizard.utils.secrets import redact
from axion_wizard.utils.shell import CommandNotFoundError, CommandResult, run, run_streaming

DEFAULT_TIMEOUT = 30.0
DEFAULT_UP_TIMEOUT = 900.0
DEFAULT_LOG_TAIL_LINES = 30


@dataclass
class ContainerStatus:
    service: str
    name: str
    state: str  # "running", "exited", "created", ...
    health: str | None  # "healthy" | "unhealthy" | "starting" | None (sin healthcheck)
    image: str | None = None
    #: puertos publicados del host reportados por Docker (campo `Publishers`
    #: de `ps --format json`). Vacío para servicios en `network_mode: host`
    #: — Docker no gestiona sus puertos, así que no aparecen aquí (§4.2/§6.1).
    published_ports: list[int] = field(default_factory=list)

    @property
    def is_running(self) -> bool:
        return self.state == "running"

    @property
    def is_healthy_or_no_healthcheck(self) -> bool:
        return self.health in (None, "healthy")

    @property
    def never_started(self) -> bool:
        """`True` si Compose creó el contenedor pero nunca llegó a arrancarlo.

        La causa habitual es un `depends_on: condition: service_healthy` que
        no llegó a tiempo: Compose no arranca un servicio hasta que sus
        dependencias están sanas, así que este contenedor puede llevar ahí
        el mismo rato que el resto sin haber ejecutado una sola línea.
        """
        return self.state == "created"


def _compose_base_args(compose_path: Path) -> list[str]:
    return ["docker", "compose", "-f", str(compose_path)]


#: Alias retenido por compatibilidad con los tests y llamadas existentes; la
#: implementación única vive en `utils.jsonio` (la compartían tres módulos).
_parse_json_lines_or_array = parse_json_lines_or_array


def config_validate(compose_path: Path, timeout: float = DEFAULT_TIMEOUT) -> None:
    """`docker compose config --quiet` — validación semántica real exigida
    por §4.5 antes de desplegar. Requiere Docker instalado."""
    try:
        result = run(
            [*_compose_base_args(compose_path), "config", "--quiet"],
            cwd=str(compose_path.parent),
            timeout=timeout,
        )
    except CommandNotFoundError as exc:
        raise ConfigError(
            what="No se encontró Docker para validar el docker-compose.yml generado",
            why="Sin Docker instalado no se puede confirmar que el archivo sea válido.",
            steps=["Instalar Docker Desktop (Windows) o Docker Engine (Linux) y reintentar."],
        ) from exc
    if not result.ok:
        raise ConfigError(
            what=f"`docker compose config` rechazó {compose_path.name}",
            # Redactado: Compose interpola el `.env` antes de validar, así que
            # su stderr puede citar una línea ya con la contraseña sustituida.
            why=redact(result.stderr.strip()) or "Docker no dio más detalles en stderr.",
            steps=["Revisar la sintaxis del docker-compose.yml generado."],
        )


def up(
    compose_path: Path,
    on_line: Callable[[str], None] | None = None,
    timeout: float = DEFAULT_UP_TIMEOUT,
    services: list[str] | None = None,
) -> CommandResult:
    """`docker compose up -d --build`. Si se pasa `on_line`, la salida se
    transmite línea a línea (para mapearla a barras de progreso de Rich).

    `services` restringe la operación a esos nombres — Compose por sí solo
    ya detecta cuándo la configuración *resuelta* de un servicio cambió
    (p.ej. una variable de entorno interpolada desde `.env`) y lo recrea
    aunque no se pida `--build` ni `--force-recreate`; no hace falta ninguno
    de los dos aquí, solo evitar tocar servicios que no cambiaron.
    """
    args = [*_compose_base_args(compose_path), "up", "-d", "--build"]
    if services:
        args.extend(services)
    if on_line is not None:
        return run_streaming(args, on_line=on_line, timeout=timeout, cwd=str(compose_path.parent))
    return run(args, timeout=timeout, cwd=str(compose_path.parent))


def restart(compose_path: Path, service: str, timeout: float = 60.0) -> CommandResult:
    """`docker compose restart <servicio>`.

    No relee el compose ni el `.env` —para eso hace falta `up`—, pero sí
    reinicia el proceso de dentro, que es lo que hace falta cuando lo que
    cambió no forma parte de la configuración del contenedor: los archivos
    montados por bind mount y las IPs que el proceso resolvió al arrancar
    (ver `s06_deploy.refresh_nginx`).
    """
    args = [*_compose_base_args(compose_path), "restart", service]
    return run(args, timeout=timeout, cwd=str(compose_path.parent))


def down(compose_path: Path, volumes: bool = False, timeout: float = 120.0) -> CommandResult:
    args = [*_compose_base_args(compose_path), "down"]
    if volumes:
        args.append("--volumes")
    return run(args, timeout=timeout, cwd=str(compose_path.parent))


def parse_ps_output(output: str) -> list[ContainerStatus]:
    entries = parse_json_lines_or_array(output)
    statuses = []
    for entry in entries:
        health = entry.get("Health") or None  # compose reporta "" cuando no hay healthcheck
        publishers = entry.get("Publishers") or []
        published_ports = sorted(
            {
                p["PublishedPort"]
                for p in publishers
                if isinstance(p, dict) and p.get("PublishedPort")
            }
        )
        statuses.append(
            ContainerStatus(
                service=entry.get("Service", ""),
                name=entry.get("Name", ""),
                state=entry.get("State", ""),
                health=health,
                image=entry.get("Image") or None,
                published_ports=published_ports,
            )
        )
    return statuses


def ps(compose_path: Path, timeout: float = DEFAULT_TIMEOUT) -> list[ContainerStatus]:
    args = [*_compose_base_args(compose_path), "ps", "--format", "json", "--all"]
    result = run(args, timeout=timeout, cwd=str(compose_path.parent))
    if not result.ok:
        return []
    return parse_ps_output(result.stdout)


def get_service_status(
    compose_path: Path, service: str, timeout: float = DEFAULT_TIMEOUT
) -> ContainerStatus | None:
    for status in ps(compose_path, timeout=timeout):
        if status.service == service:
            return status
    return None


def logs(
    compose_path: Path, service: str, tail: int = DEFAULT_LOG_TAIL_LINES, timeout: float = 30.0
) -> str:
    """Log de un servicio, ya redactado.

    La redacción se aplica aquí, en la frontera, y no en cada punto de
    impresión: PostgreSQL y Mattermost registran su DSN completo —con la
    contraseña— cuando fallan al conectar, y este log acaba tanto en el
    panel de error de §4.6 como en el archivo de log del wizard.

    Si el propio comando `docker compose logs` falla —el servicio no existe
    en el compose, el daemon no responde— se devuelve ese error en vez de
    una cadena vacía indistinguible de "el contenedor no escribió nada".
    """
    args = [*_compose_base_args(compose_path), "logs", "--no-color", "--tail", str(tail), service]
    result = run(args, timeout=timeout, cwd=str(compose_path.parent))
    if not result.ok:
        fallback = f"`docker compose logs` salió con {result.returncode}"
        detail = redact(result.stderr.strip()) or fallback
        return f"[no se pudo leer el log: {detail}]"
    return redact(result.stdout)


def exec_in_service(
    compose_path: Path, service: str, command: list[str], timeout: float = 30.0
) -> CommandResult:
    args = [*_compose_base_args(compose_path), "exec", "-T", service, *command]
    return run(args, timeout=timeout, cwd=str(compose_path.parent))


def describe_service_state(status: ContainerStatus | None, service: str) -> str:
    """Explica por qué un servicio no está operativo, más allá del log.

    Existe porque un log vacío es ambiguo por sí solo: puede ser que el
    contenedor haya arrancado y realmente no escriba nada, o que nunca haya
    llegado a arrancar —en cuyo caso "sin salida en el log" es literalmente
    correcto pero no dice la causa real, que casi siempre es una dependencia
    (`depends_on: condition: service_healthy`) que no llegó a tiempo. Sin
    esto, diagnosticarlo exige un `docker compose ps` aparte para ver qué
    estado tiene realmente cada contenedor.
    """
    if status is None:
        return (
            f"El servicio `{service}` ni siquiera se llegó a crear — Compose no "
            "arranca un servicio hasta que sus dependencias (`depends_on`) están "
            "listas."
        )
    if status.never_started:
        return (
            f"El contenedor de `{service}` se creó pero nunca llegó a arrancar; "
            "por eso no hay log que mostrar. La causa habitual es una "
            "dependencia (`depends_on: condition: service_healthy`) que no "
            "llegó a estar sana a tiempo."
        )
    if not status.is_running:
        return f"El contenedor de `{service}` arrancó y terminó (estado: `{status.state}`)."
    if not status.is_healthy_or_no_healthcheck:
        return (
            f"El contenedor de `{service}` está corriendo pero su healthcheck "
            f"no pasa (estado: `{status.health}`)."
        )
    return f"El contenedor de `{service}` está corriendo y sano."


def build_deployment_failure_error(compose_path: Path, service: str) -> DeploymentError:
    """Arma un `DeploymentError` con el estado del contenedor y las últimas
    `DEFAULT_LOG_TAIL_LINES` líneas de su log, tal como exige §4.6."""
    status = get_service_status(compose_path, service)
    state_note = describe_service_state(status, service)
    tail = logs(compose_path, service, tail=DEFAULT_LOG_TAIL_LINES).strip()
    tail = tail or "(sin salida en el log)"

    steps = [f"Revisar el log completo: docker compose -f {compose_path} logs {service}"]
    if status is None or status.never_started:
        # El log de ESTE servicio no va a decir nada útil: el problema está
        # en la dependencia que lo bloquea, así que se apunta a mirar el
        # conjunto antes de perder tiempo en un contenedor que nunca corrió.
        steps.insert(
            0, f"Ver el estado de todos los servicios: docker compose -f {compose_path} ps"
        )
    steps.append(f"Reintentar tras corregir el problema: axion-wizard up {service}")

    return DeploymentError(
        what=f"El servicio `{service}` no llegó a estar operativo",
        why=f"{state_note}\n\nÚltimas {DEFAULT_LOG_TAIL_LINES} líneas de su log:\n\n{tail}",
        steps=steps,
    )
