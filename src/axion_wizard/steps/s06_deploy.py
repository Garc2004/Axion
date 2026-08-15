"""Step 6 — Deployment (§4.6).

- `docker compose up -d --build`, with the streamed output mapped onto Rich
  progress bars (one per service).
- Healthcheck waiting with `tenacity`: retries with exponential backoff and a
  configurable global timeout (300s by default).
- `ollama` has no healthcheck of its own: its internal HTTP server is
  confirmed to answer by running `ollama list` inside the container — which
  internally performs the same `GET /api/tags` the spec requires, without
  depending on the image shipping `curl`/`wget`.
- On failure, `compose.build_deployment_failure_error` assembles the error
  with the last 30 lines of the failed container's log.
- After deploying, the effective wg-easy tag is verified (§6.4).
"""

from __future__ import annotations

import re
from pathlib import Path

from rich.progress import BarColumn, Progress, SpinnerColumn, TaskID, TextColumn
from tenacity import RetryError, Retrying, retry_if_result, stop_after_delay, wait_exponential

from axion_wizard.domain import images
from axion_wizard.domain.stack import (
    MANAGED_SERVICES,
    NGINX_SERVICE,
    NGINX_UPSTREAM_SERVICES,
    OLLAMA_SERVICE,
    WIREGUARD_SERVICE,
)
from axion_wizard.errors import DeploymentError
from axion_wizard.render.console import console
from axion_wizard.services import compose
from axion_wizard.steps.base import Step, StepResult
from axion_wizard.utils.shell import CommandNotFoundError, CommandTimeoutError

#: A single source of truth with `services.compose`: the deployment's limit is
#: a property of the command, not of the step that happens to call it.
DEFAULT_UP_TIMEOUT = compose.DEFAULT_UP_TIMEOUT
DEFAULT_HEALTHCHECK_TIMEOUT = 300.0

_DONE_STATUSES = frozenset({"Started", "Healthy", "Running", "Built"})

#: `Image` as well as `Container`: `docker compose up --build` announces the
#: build as ` Image axion-fastapi Building ` and ` Image axion-fastapi Built `,
#: and without that alternative the regex read `Image` as the name, looked for a
#: status right after it, found the image's name instead and matched nothing —
#: so the two events that frame the longest phase of a first install were the
#: ones being dropped. The image name carries the service's, which is all
#: `_match_task` needs.
_PROGRESS_LINE_RE = re.compile(
    r"^\s*(?:(?:Container|Image)\s+)?(?P<name>\S+)\s+(?P<status>Pulling|Pulled|Building|Built|"
    r"Creating|Created|Starting|Started|Waiting|Healthy|Running|Removing|Removed|"
    r"Stopping|Stopped|Error)\b"
)

#: Buildkit's own progress, which names no service: `#7 [4/5] RUN pip install …`,
#: `#11 exporting layers`. See `parse_buildkit_step_line`.
_BUILDKIT_STEP_RE = re.compile(r"^#\d+\s+(?P<detail>(?:\[[^\]]*\]|exporting)\s*\S.*)$")

#: Buildkit details are long (a whole `RUN` line) and share the row with the
#: bar; past this they are cut with an ellipsis.
_MAX_STATUS_LENGTH = 44


def parse_progress_line(line: str) -> tuple[str, str] | None:
    """Extract `(container_or_service_name, status)` from a typical line of
    `docker compose up --build` (e.g. `Container axion-postgres-1  Started`).
    Returns `None` if the line does not match the expected format."""
    match = _PROGRESS_LINE_RE.match(line)
    if not match:
        return None
    return match.group("name"), match.group("status")


def parse_buildkit_step_line(line: str) -> str | None:
    """The readable part of a buildkit progress line, or `None`.

    A first install spends most of step 6 inside buildkit, and buildkit says
    nothing about services: of the 48 lines a minimal build emits, 30 look like
    `#6 [2/2] RUN …` and `parse_progress_line` matches not one of them. The
    result was that every bar sat at "waiting…" for the whole build while
    Docker Desktop still showed no containers — indistinguishable, from the
    outside, from the wizard having hung. This is a real incident, and the
    reason the wizard reports what buildkit is doing instead of ignoring it.

    Which bar it belongs to cannot be read from the line itself; `deploy`
    routes it to whichever service the preceding ` Image … Building ` named.
    """
    match = _BUILDKIT_STEP_RE.match(line.strip())
    if not match:
        return None
    return match.group("detail").strip() or None


def shorten_status(detail: str, limit: int = _MAX_STATUS_LENGTH) -> str:
    if len(detail) <= limit:
        return detail
    return f"{detail[: limit - 1].rstrip()}…"


def _match_task(tasks: dict[str, TaskID], container_or_service_name: str) -> TaskID | None:
    """`docker compose` reports container names shaped
    `<project>-<service>-<n>`; find which managed service appears as a
    substring."""
    for service_name, task_id in tasks.items():
        if service_name in container_or_service_name:
            return task_id
    return None


def deploy(compose_path: Path, services: list[str], timeout: float = DEFAULT_UP_TIMEOUT) -> None:
    """`docker compose up -d --build` with one progress bar per service.
    Raises `DeploymentError` carrying the failed container's log if the
    command exits non-zero, or `compose.build_up_timeout_error` if it runs out
    of time."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}", justify="left"),
        BarColumn(),
        TextColumn("{task.fields[status]}"),
        console=console,
    ) as progress:
        tasks: dict[str, TaskID] = {
            name: progress.add_task(name, total=1, status="waiting…") for name in services
        }
        #: The bar buildkit's output belongs to: whichever service the last
        #: ` Image … Building ` named.
        building: TaskID | None = None

        def on_line(line: str) -> None:
            nonlocal building
            parsed = parse_progress_line(line)
            if parsed is not None:
                name, status = parsed
                task_id = _match_task(tasks, name)
                if task_id is not None:
                    if status == "Building":
                        building = task_id
                    elif status == "Built":
                        building = None
                    completed = 1 if status in _DONE_STATUSES else 0
                    progress.update(task_id, status=status, completed=completed)
                return

            # Not a compose event: it may still be buildkit reporting the step
            # it is on, which is the only sign of life during a build.
            detail = parse_buildkit_step_line(line)
            if detail is not None and building is not None:
                progress.update(building, status=shorten_status(detail))

        try:
            result = compose.up(compose_path, on_line=on_line, timeout=timeout)
        except CommandTimeoutError as exc:
            raise compose.build_up_timeout_error(compose_path, timeout) from exc

    if not result.ok:
        failed_service = _first_not_running_service(compose_path, services) or services[0]
        raise compose.build_deployment_failure_error(compose_path, failed_service)


def refresh_nginx(compose_path: Path, services: list[str]) -> bool:
    """Restart nginx after recreating one of its upstreams, so it re-reads the
    certificate and finds Mattermost again immediately.

    The underlying reason — nginx holding on to the first IP it resolved and
    leaving the whole stack on 502 — is already headed off by nginx's own
    configuration, which re-resolves on every request (see
    `nginx-mattermost.conf.j2`). This is still needed for two things that does
    not cover:

    1. The certificate. `nginx/certs` comes in through a bind mount, so the
       new file is already inside the container, but nginx serves the one it
       loaded into memory at startup. An `install` that regenerated the
       certificate left it with no effect until the next restart.
    2. The re-resolution gap. The `resolver` caches for a few seconds; without
       this restart, an `install` can finish announcing everything is ready
       while the next few requests still return 502.

    Returns whether a restart was needed.
    """
    if not NGINX_UPSTREAM_SERVICES.intersection(services):
        # No upstream was touched: the IP nginx has resolved is still valid.
        # If nginx itself was what got recreated, it started after its
        # upstreams and has already resolved correctly.
        return False

    status = compose.get_service_status(compose_path, NGINX_SERVICE)
    if status is None or not status.is_running:
        # With nginx not running there is nothing to refresh; if it needs
        # bringing up, it will start with the current config and IPs.
        return False

    result = compose.restart(compose_path, NGINX_SERVICE)
    if not result.ok:
        raise compose.build_deployment_failure_error(compose_path, NGINX_SERVICE)
    return True


def _first_not_running_service(compose_path: Path, services: list[str]) -> str | None:
    statuses = _service_statuses(compose_path)
    for name in services:
        status = statuses.get(name)
        if status is None or not status.is_running:
            return name
    return None


def check_ollama_ready(
    compose_path: Path, service: str = OLLAMA_SERVICE, timeout: float = 10.0
) -> bool:
    """`ollama list` internally queries the same endpoint as `GET /api/tags`;
    using it avoids depending on the image shipping HTTP tools.

    A timeout is answered with `False`, not with an exception: this runs inside
    `wait_for_healthy`'s retry loop, where "it did not answer in ten seconds"
    means precisely "not ready yet" and the next round will ask again. Letting
    `CommandTimeoutError` escape instead aborted the whole step over one slow
    reply — and because it is not an `AxionError`, it did so without recording
    the failure and under the `Unexpected error` handler.
    """
    try:
        result = compose.exec_in_service(
            compose_path, service, ["ollama", "list"], timeout=timeout
        )
    except (CommandTimeoutError, CommandNotFoundError):
        return False
    return result.ok


def _service_is_ready(
    compose_path: Path, service: str, statuses: dict[str, compose.ContainerStatus]
) -> bool:
    status = statuses.get(service)
    if status is None or not status.is_running:
        return False
    if service == OLLAMA_SERVICE:
        return check_ollama_ready(compose_path, service)
    return status.is_healthy_or_no_healthcheck


def _service_statuses(compose_path: Path) -> dict[str, compose.ContainerStatus]:
    return {status.service: status for status in compose.ps(compose_path)}


def _all_services_ready(compose_path: Path, services: list[str]) -> bool:
    """One `docker compose ps` per round, not one per service.

    `get_service_status` internally runs a full `ps` and keeps a single row,
    so asking service by service multiplied the number of Docker invocations
    per retry by six — and this loops with backoff for up to five minutes.
    With one `ps` per round the information is exactly the same, and
    consistent across services too: before, each query saw a different
    instant.
    """
    statuses = _service_statuses(compose_path)
    return all(_service_is_ready(compose_path, name, statuses) for name in services)


def wait_for_healthy(
    compose_path: Path,
    services: list[str],
    timeout: float = DEFAULT_HEALTHCHECK_TIMEOUT,
    wait_min: float = 1.0,
    wait_max: float = 15.0,
) -> None:
    """Retry with exponential backoff until every service in `services`
    reports ready, or until `timeout` seconds have elapsed in total.
    `wait_min`/`wait_max` bound the backoff — configurable so the retry logic
    can be tested without real multi-second waits."""
    retryer = Retrying(
        stop=stop_after_delay(timeout),
        wait=wait_exponential(multiplier=wait_min, min=wait_min, max=wait_max),
        retry=retry_if_result(lambda ready: ready is False),
        reraise=False,
    )
    try:
        retryer(_all_services_ready, compose_path, services)
    except RetryError as exc:
        failed_service = _first_not_ready_service(compose_path, services) or services[0]
        raise compose.build_deployment_failure_error(compose_path, failed_service) from exc


def _first_not_ready_service(compose_path: Path, services: list[str]) -> str | None:
    statuses = _service_statuses(compose_path)
    for name in services:
        if not _service_is_ready(compose_path, name, statuses):
            return name
    return None


def verify_panel_password_reached_the_container(
    compose_path: Path, expected: str, service: str = WIREGUARD_SERVICE, timeout: float = 20.0
) -> None:
    """Check that wg-easy received the panel password intact.

    It is read from the running container rather than trusting what was
    written into `wg.env`, for the same reason the certificate's SAN is read
    back from the file: between what one side writes and what the other sees
    there is a layer that can transform it. That layer is Docker Compose's
    variable interpolation, which eats any unescaped `$`.

    Under v14 this caught a real and frequent incident: the bcrypt hash starts
    with `$2b$` and arrived mangled. v15 receives the password in the clear
    and the wizard forbids `$` in it, so the failure should no longer be
    possible — the check stays because it is cheap and because the way it
    would fail remains the worst possible one: the container starts healthy,
    the panel answers, and the only thing that happens is that no password
    ever works.
    """
    result = compose.exec_in_service(
        compose_path, service, ["printenv", "INIT_PASSWORD"], timeout=timeout
    )
    if not result.ok:
        # We could not ask (a container without `printenv`, exec denied).
        # That is no reason to declare the deployment failed.
        return

    received = result.stdout.strip()
    if received == expected:
        return

    raise DeploymentError(
        what="The WireGuard panel received a different password from the one configured",
        why=(
            f"`INIT_PASSWORD` reached the container with {len(received)} characters "
            f"instead of {len(expected)}: Docker Compose interpolates the values of "
            "`env_file:`, so an unescaped `$` is read as a variable. With the password "
            "altered, the panel starts and answers normally but rejects every login, "
            "writing nothing to its logs."
        ),
        steps=[
            "Regenerate the project files: axion-wizard install",
            "Choose a panel password without `$`, backtick or `!`.",
            f"Recreate the container: axion-wizard up {service}",
        ],
    )


def verify_wg_easy_tag(compose_path: Path, service: str = WIREGUARD_SERVICE) -> None:
    """Verify the *effective* tag of the deployed wg-easy container (§6.4),
    not only the one written into the generated `docker-compose.yml`."""
    status = compose.get_service_status(compose_path, service)
    if status is None or not status.image:
        return
    _repo, tag = images.split_image_tag(status.image)
    # With no explicit tag Docker resolves to `latest`, whatever that points
    # at today — the exact case §6.4 exists to head off, so it is treated as
    # one.
    effective_tag = tag if tag is not None else "latest"
    try:
        images.assert_wg_easy_tag_is_safe(effective_tag)
    except images.UnsafeWgEasyTagError as exc:
        raise DeploymentError(
            what=f"wg-easy is running an unsafe tag: {status.image}",
            why=str(exc),
            steps=[
                "Stop the stack: axion-wizard down",
                f"Check docker-compose.yml pins the image to {images.WIREGUARD_IMAGE}",
                "Deploy again: axion-wizard up",
            ],
        ) from exc


class DeployStep(Step):
    """Bring the stack up and wait until it is genuinely operational (§4.6).

    "Started" is not the same as "ready": `docker compose up -d` returns as
    soon as the containers exist, long before PostgreSQL accepts connections
    or Mattermost answers. Hence waiting on the healthchecks with backoff
    after deploying, before taking the step as done.
    """

    name = "deploy"
    title = "Deployment"

    def run(self) -> StepResult:
        compose_path = self.context.project_dir / "docker-compose.yml"
        services = list(MANAGED_SERVICES)

        if self.state.dry_run:
            console.print(
                "[axion.info][dry-run][/] would run `docker compose up -d --build` "
                f"for {', '.join(services)}"
            )
            self._ensure_host_ip_forwarding()
            return StepResult(name=self.name, ok=True, message="skipped by --dry-run")

        self._ensure_host_ip_forwarding()
        deploy(compose_path, services=services)
        wait_for_healthy(compose_path, services=services)
        # Before declaring the deployment good: if Mattermost was recreated,
        # nginx still points at the previous IP and the whole stack returns
        # 502 despite all six containers being healthy.
        if refresh_nginx(compose_path, services):
            console.print("[axion.ok]nginx restarted[/] so it can find Mattermost again.")
        # The tag that matters is the running container's, not the one written
        # into the compose file: anyone hand-editing it can end up on another
        # wg-easy major, which configures itself differently and fails without
        # a single error in the logs (§6.4).
        verify_wg_easy_tag(compose_path)
        # And the password that matters is the one the container received, not
        # the one written into wg.env: Compose interpolates env_file values.
        verify_panel_password_reached_the_container(
            compose_path,
            expected=self.context.require_config().wireguard_admin_password.get_secret_value(),
        )

        console.print("[axion.ok]Stack up and healthy.[/]")
        return StepResult(
            name=self.name, ok=True, message=f"{len(services)} services operational"
        )

    def _ensure_host_ip_forwarding(self) -> None:
        """Enable the host's IP forwarding before bringing anything up (§6.1).

        Applies only to native Linux with `network_mode: host`. It comes
        *before* the deployment because it is host kernel configuration, not
        container configuration: with forwarding off, wg-easy starts without
        complaint, the tunnel establishes and the handshake works — but no
        packet reaches its destination, without a single error in any log.

        A failure here does not abort: the VPN would be left unable to route,
        but Mattermost, the AI and LAN access work regardless. It warns with
        the manual steps and carries on.
        """
        from axion_wizard.services import hostnet

        environment = self.context.require_environment()
        if not hostnet.is_applicable(
            environment.os_info.name, environment.wireguard_variant
        ):
            return

        result = hostnet.ensure_ip_forwarding(dry_run=self.state.dry_run)

        if self.state.dry_run:
            console.print(f"[axion.info][dry-run][/] {result.detail}")
            return
        if not result.needs_attention:
            console.print(f"[axion.ok]Host IP forwarding:[/] {result.detail}")
            return

        message = (
            "The host's IP forwarding did not end up active "
            f"({result.detail}). The WireGuard tunnel will establish and the panel "
            "will show the client as connected, but no packet will reach its "
            "destination — and no error will appear in any log."
        )
        self.warn_and_show(message)
        for step in hostnet.describe_manual_fix():
            console.print(f"[axion.dim]  - {step}[/]")

    def verify(self) -> StepResult:
        from axion_wizard.services import compose as compose_service

        if self.state.dry_run:
            return StepResult(name=self.name, ok=True, message="skipped by --dry-run")

        compose_path = self.context.project_dir / "docker-compose.yml"
        statuses = {s.service: s for s in compose_service.ps(compose_path)}
        unhealthy = [
            name
            for name in MANAGED_SERVICES
            if name not in statuses
            or not statuses[name].is_running
            or not statuses[name].is_healthy_or_no_healthcheck
        ]
        if unhealthy:
            return StepResult(
                name=self.name, ok=False, message=f"not operational: {', '.join(unhealthy)}"
            )
        return StepResult(name=self.name, ok=True, message="all services operational")
