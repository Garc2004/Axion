"""A subprocess wrapper around `docker compose` (§4.6, §1.3).

Invoking `docker compose` through a subprocess is more reliable and more
transparent than the docker-py SDK, whose Compose v2 support is poor — hence
this module being a thin layer over `utils.shell`, with no dependency on the
`docker` package.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from axion_wizard.errors import ConfigError, DeploymentError
from axion_wizard.utils.jsonio import parse_json_lines_or_array
from axion_wizard.utils.secrets import redact
from axion_wizard.utils.shell import (
    CommandNotFoundError,
    CommandResult,
    CommandTimeoutError,
    run,
    run_streaming,
)

DEFAULT_TIMEOUT = 30.0

#: Limit for `up -d --build`, which on a first install has to cover pulling the
#: whole stack — ollama alone is over 6 GB, and the eight images together come
#: to around eleven.
#:
#: It used to be 900 s, and that is not a margin: **without a TTY, Docker emits
#: no byte-level progress**. Measured against this same daemon, a pull writes
#: only state transitions (`Pulling fs layer` → `Download complete` → `Pull
#: complete`), so one large layer is a *single* silent stretch lasting as long
#: as the download does. The limit therefore has to cover the entire transfer,
#: not a lull in it: on a domestic line those eleven gigabytes take well over
#: fifteen minutes, and the wizard was killing its own deployment halfway
#: through and blaming the timeout.
#:
#: The other half of that failure — that giving up used to be indistinguishable
#: from hanging — is handled by `build_up_timeout_error`.
DEFAULT_UP_TIMEOUT = 3600.0

DEFAULT_LOG_TAIL_LINES = 30


@dataclass
class ContainerStatus:
    service: str
    name: str
    state: str  # "running", "exited", "created", …
    health: str | None  # "healthy" | "unhealthy" | "starting" | None (no healthcheck)
    image: str | None = None
    #: Host ports published as reported by Docker (the `Publishers` field of
    #: `ps --format json`). Empty for services on `network_mode: host` —
    #: Docker does not manage their ports, so they do not appear here
    #: (§4.2/§6.1).
    published_ports: list[int] = field(default_factory=list)

    @property
    def is_running(self) -> bool:
        return self.state == "running"

    @property
    def is_healthy_or_no_healthcheck(self) -> bool:
        return self.health in (None, "healthy")

    @property
    def never_started(self) -> bool:
        """`True` if Compose created the container but never started it.

        The usual cause is a `depends_on: condition: service_healthy` that did
        not come good in time: Compose will not start a service until its
        dependencies are healthy, so this container can have been sitting
        there as long as the rest without executing a single line.
        """
        return self.state == "created"


def _compose_base_args(compose_path: Path) -> list[str]:
    return ["docker", "compose", "-f", str(compose_path)]


#: Alias kept for compatibility with existing tests and callers; the single
#: implementation lives in `utils.jsonio` (three modules shared it).
_parse_json_lines_or_array = parse_json_lines_or_array


def config_validate(compose_path: Path, timeout: float = DEFAULT_TIMEOUT) -> None:
    """`docker compose config --quiet` — the real semantic validation §4.5
    requires before deploying. Needs Docker installed."""
    try:
        result = run(
            [*_compose_base_args(compose_path), "config", "--quiet"],
            cwd=str(compose_path.parent),
            timeout=timeout,
        )
    except CommandNotFoundError as exc:
        raise ConfigError(
            what="Docker was not found, so the generated docker-compose.yml cannot be validated",
            why="Without Docker installed there is no way to confirm the file is valid.",
            steps=["Install Docker Desktop (Windows) or Docker Engine (Linux) and retry."],
        ) from exc
    if not result.ok:
        raise ConfigError(
            what=f"`docker compose config` rejected {compose_path.name}",
            # Redacted: Compose interpolates the `.env` before validating, so
            # its stderr can quote a line with the password already
            # substituted in.
            why=redact(result.stderr.strip()) or "Docker gave no further detail on stderr.",
            steps=["Check the syntax of the generated docker-compose.yml."],
        )


def up(
    compose_path: Path,
    on_line: Callable[[str], None] | None = None,
    timeout: float = DEFAULT_UP_TIMEOUT,
    services: list[str] | None = None,
) -> CommandResult:
    """`docker compose up -d --build`. If `on_line` is given, the output is
    streamed line by line (to map onto Rich progress bars).

    `services` restricts the operation to those names — Compose already
    detects on its own when a service's *resolved* configuration changed (an
    environment variable interpolated from `.env`, say) and recreates it
    without being asked for `--build` or `--force-recreate`; neither is needed
    here, only avoiding services that did not change.
    """
    args = [*_compose_base_args(compose_path), "up", "-d", "--build"]
    if services:
        args.extend(services)
    if on_line is not None:
        return run_streaming(args, on_line=on_line, timeout=timeout, cwd=str(compose_path.parent))
    return run(args, timeout=timeout, cwd=str(compose_path.parent))


def restart(compose_path: Path, service: str, timeout: float = 60.0) -> CommandResult:
    """`docker compose restart <service>`.

    It re-reads neither the compose file nor the `.env` — that takes `up` —
    but it does restart the process inside, which is what is needed when what
    changed is not part of the container's configuration: bind-mounted files,
    and the IPs the process resolved at startup (see
    `s06_deploy.refresh_nginx`).
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
        health = entry.get("Health") or None  # compose reports "" when there is no healthcheck
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
    """The state of the project's containers, or an empty list if it could not
    be read.

    A timeout counts as "could not be read" rather than as an error to raise.
    This is called from inside `s06_deploy.wait_for_healthy`'s retry loop and
    from every `doctor` check, and in both places an empty list already means
    "nothing is operational" and produces the right message. Raising instead
    meant a `CommandTimeoutError` — not an `AxionError` — escaping through the
    generic handler, so a daemon that answered slowly once looked like a wizard
    crash and left nothing recorded in the state file.
    """
    args = [*_compose_base_args(compose_path), "ps", "--format", "json", "--all"]
    try:
        result = run(args, timeout=timeout, cwd=str(compose_path.parent))
    except (CommandNotFoundError, CommandTimeoutError):
        return []
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
    """A service's log, already redacted.

    Redaction is applied here, at the boundary, rather than at each print
    site: PostgreSQL and Mattermost log their full DSN — password included —
    when they fail to connect, and this log ends up both in §4.6's error panel
    and in the wizard's log file.

    If the `docker compose logs` command itself fails — the service is not in
    the compose file, the daemon is not answering — that error is returned
    instead of an empty string indistinguishable from "the container wrote
    nothing".
    """
    args = [*_compose_base_args(compose_path), "logs", "--no-color", "--tail", str(tail), service]
    result = run(args, timeout=timeout, cwd=str(compose_path.parent))
    if not result.ok:
        fallback = f"`docker compose logs` exited with {result.returncode}"
        detail = redact(result.stderr.strip()) or fallback
        return f"[could not read the log: {detail}]"
    return redact(result.stdout)


def exec_in_service(
    compose_path: Path, service: str, command: list[str], timeout: float = 30.0
) -> CommandResult:
    args = [*_compose_base_args(compose_path), "exec", "-T", service, *command]
    return run(args, timeout=timeout, cwd=str(compose_path.parent))


def describe_service_state(status: ContainerStatus | None, service: str) -> str:
    """Explain why a service is not operational, beyond its log.

    This exists because an empty log is ambiguous on its own: the container
    may have started and genuinely written nothing, or it may never have
    started at all — in which case "no output in the log" is literally correct
    but says nothing about the real cause, which is almost always a dependency
    (`depends_on: condition: service_healthy`) that did not come good in time.
    Without this, diagnosing it takes a separate `docker compose ps` to see
    what state each container is actually in.
    """
    if status is None:
        return (
            f"Service `{service}` was never even created — Compose does not start "
            "a service until its dependencies (`depends_on`) are ready."
        )
    if status.never_started:
        return (
            f"The container for `{service}` was created but never started, which "
            "is why there is no log to show. The usual cause is a dependency "
            "(`depends_on: condition: service_healthy`) that did not become "
            "healthy in time."
        )
    if not status.is_running:
        return f"The container for `{service}` started and exited (state: `{status.state}`)."
    if not status.is_healthy_or_no_healthcheck:
        return (
            f"The container for `{service}` is running but its healthcheck is not "
            f"passing (state: `{status.health}`)."
        )
    return f"The container for `{service}` is running and healthy."


def build_up_timeout_error(compose_path: Path, timeout: float) -> DeploymentError:
    """Assemble the error for a `docker compose up` that ran out of time.

    It exists because the alternative was letting `CommandTimeoutError` travel
    up, and that is a plain `RuntimeError`: `run_steps` only records
    `AxionError`, so the failure never reached `.axion-wizard-state.json` — the
    state file stayed pointing at step 5 while the run had already given up on
    step 6 — and it came out through the last-resort handler as `Unexpected
    error: 900.0s timeout exceeded running: docker compose …`, exactly the raw
    message §8 exists to prevent. From a real incident: forty minutes with the
    eight containers up and healthy and a wizard that said nothing about it.

    The containers are deliberately described as possibly fine: what timed out
    is the command the wizard was watching, not necessarily the deployment, and
    sending someone to tear down a stack that is already running would be the
    wrong move.
    """
    running = [status.service for status in ps(compose_path) if status.is_running]
    if running:
        state_note = (
            f"These containers are up despite it: {', '.join(sorted(running))}. What ran "
            "out of time is the command the wizard was watching, not necessarily the "
            "deployment itself."
        )
    else:
        state_note = "No container from this project is up."

    return DeploymentError(
        what=f"`docker compose up` exceeded its {timeout:.0f}s limit",
        why=(
            f"{state_note}\n\nOn a first install this command downloads the whole stack "
            "(around eleven gigabytes) and builds the FastAPI bridge, and Docker reports "
            "no progress at all while a layer is downloading — so a slow connection can "
            "look identical to a stall."
        ),
        steps=[
            f"See what did come up: docker compose -f {compose_path} ps",
            "Resume where it left off: axion-wizard install — already downloaded layers "
            "are cached, so the retry starts from where the download got to.",
            "If it times out again with nothing downloading, read the log of whichever "
            f"service is missing: docker compose -f {compose_path} logs <service>",
        ],
    )


def build_deployment_failure_error(compose_path: Path, service: str) -> DeploymentError:
    """Assemble a `DeploymentError` carrying the container's state and the
    last `DEFAULT_LOG_TAIL_LINES` lines of its log, as §4.6 requires."""
    status = get_service_status(compose_path, service)
    state_note = describe_service_state(status, service)
    tail = logs(compose_path, service, tail=DEFAULT_LOG_TAIL_LINES).strip()
    tail = tail or "(no output in the log)"

    steps = [f"Read the full log: docker compose -f {compose_path} logs {service}"]
    if status is None or status.never_started:
        # THIS service's log will say nothing useful: the problem is in the
        # dependency blocking it, so point at the whole picture before anyone
        # wastes time on a container that never ran.
        steps.insert(
            0, f"See the state of every service: docker compose -f {compose_path} ps"
        )
    steps.append(f"Retry once the problem is fixed: axion-wizard up {service}")

    return DeploymentError(
        what=f"Service `{service}` never became operational",
        why=f"{state_note}\n\nLast {DEFAULT_LOG_TAIL_LINES} lines of its log:\n\n{tail}",
        steps=steps,
    )
