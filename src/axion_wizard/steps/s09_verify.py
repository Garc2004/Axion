"""Step 9 — Final verification (§4.9).

The same set of checks `axion-wizard doctor` runs. Unlike the install step,
`doctor` does not depend on the persisted state of an `install` run — it
rebuilds what it needs (host, model, variant) by reading the artifacts
already written into the `project_dir` (`docker-compose.yml`, `.env`,
`wg.env`) directly, so it can diagnose a stack deployed by any means.
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass
from pathlib import Path

import httpx
from rich.table import Table
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_result,
    stop_after_delay,
    wait_exponential,
)

from axion_wizard.detect import network as detect_network
from axion_wizard.domain.config import WireguardVariant
from axion_wizard.domain.deployment import DeploymentFacts, discover_deployment
from axion_wizard.domain.stack import (
    FASTAPI_SERVICE,
    MATTERMOST_SERVICE,
    NGINX_SERVICE,
    WIREGUARD_SERVICE,
)
from axion_wizard.errors import ConfigError
from axion_wizard.render import ui
from axion_wizard.render.console import console
from axion_wizard.services import certs, compose
from axion_wizard.services import ollama as ollama_service
from axion_wizard.services.wireguard import build_panel_url
from axion_wizard.steps.base import Step, StepResult

DEFAULT_CHECK_TIMEOUT = 10.0
#: Total retry budget for the HTTP checks sensitive to LAN access latency
#: under Docker Desktop/WSL2 (§6.5) — see `_check_url_with_retry`.
#: Deliberately short: `doctor` is a quick diagnosis, not another deployment
#: wait.
DEFAULT_CHECK_RETRY_TIMEOUT = 20.0


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


# Deployment discovery lives in `axion_wizard.domain.deployment`: `doctor`,
# `wireguard add-client` and step 3's `restore()` all need it, and none of the
# three is this step. It is re-exported here so as not to break anyone who was
# already importing it from step 9's module.
__all__ = [
    "DeploymentFacts",
    "discover_deployment",
]


# --- Individual checks (the table in §4.9) -------------------------------------


def check_containers_healthy(compose_path: Path) -> CheckResult:
    """Method: `docker compose ps --format json`."""
    statuses = compose.ps(compose_path)
    if not statuses:
        return CheckResult("Containers healthy", False, "could not read `docker compose ps`")
    unhealthy = [s for s in statuses if not s.is_running or not s.is_healthy_or_no_healthcheck]
    if unhealthy:
        names = ", ".join(s.service for s in unhealthy)
        return CheckResult("Containers healthy", False, f"unhealthy: {names}")
    return CheckResult("Containers healthy", True, f"{len(statuses)} services OK")


async def _check_url_with_retry(
    name: str,
    url: str,
    *,
    verify: bool = True,
    timeout: float = DEFAULT_CHECK_TIMEOUT,
    retry_timeout: float = DEFAULT_CHECK_RETRY_TIMEOUT,
) -> CheckResult:
    """`GET url`, treating any non-5xx response as OK.

    It retries with a short backoff rather than making a single attempt: LAN
    access under Docker Desktop/WSL2 can take several seconds to answer even
    when it works perfectly (a real finding — a browser, or `install` itself,
    which does retry when creating the WireGuard client, saw the service
    without trouble while a single `doctor` attempt marked it FAIL). It masks
    no real failure: if it never answers, it still fails once `retry_timeout`
    is exhausted.
    """
    last_detail = ""

    async def _attempt() -> bool:
        nonlocal last_detail
        try:
            async with httpx.AsyncClient(timeout=timeout, verify=verify) as client:
                response = await client.get(url)
        except httpx.HTTPError as exc:
            last_detail = str(exc)
            return False
        last_detail = f"HTTP {response.status_code} at {url}"
        return response.status_code < 500

    retryer = AsyncRetrying(
        stop=stop_after_delay(retry_timeout),
        wait=wait_exponential(multiplier=1, min=1, max=5),
        retry=retry_if_result(lambda ok: ok is False),
        reraise=False,
    )
    try:
        ok: bool = await retryer(_attempt)
    except RetryError:
        ok = False
    return CheckResult(name, ok, last_detail)


async def check_https_responds(
    host: str,
    timeout: float = DEFAULT_CHECK_TIMEOUT,
    retry_timeout: float = DEFAULT_CHECK_RETRY_TIMEOUT,
) -> CheckResult:
    """Method: `GET https://<host>`, skipping verification of the self-signed cert."""
    return await _check_url_with_retry(
        "HTTPS responds",
        f"https://{host}",
        verify=False,  # noqa: S501 - self-signed cert on purpose, see §4.4
        timeout=timeout,
        retry_timeout=retry_timeout,
    )


def check_cert_has_san(cert_path: Path) -> CheckResult:
    """Method: parse it with `cryptography`."""
    if not cert_path.exists():
        return CheckResult("Cert has SAN", False, f"{cert_path} does not exist")
    try:
        san_entries = certs.verify_certificate_has_san(cert_path)
    except ConfigError as exc:
        return CheckResult("Cert has SAN", False, exc.what)
    return CheckResult("Cert has SAN", True, ", ".join(san_entries))


def check_webhook_reachable(
    compose_path: Path,
    mattermost_service: str = MATTERMOST_SERVICE,
    fastapi_service: str = FASTAPI_SERVICE,
    timeout: float = 15.0,
) -> CheckResult:
    """Method: `docker exec <mattermost>` → a request to `fastapi:8000/health`.

    `curl`, not `wget`: the official mattermost-team-edition image does not
    ship wget (the same finding as the service's own healthcheck, §4.6) — this
    check always failed with "executable file not found in $PATH",
    indistinguishable from a genuinely unreachable webhook.
    """
    result = compose.exec_in_service(
        compose_path,
        mattermost_service,
        ["curl", "-fsS", f"http://{fastapi_service}:8000/health"],
        timeout=timeout,
    )
    detail = result.stdout.strip() or result.stderr.strip()
    return CheckResult("Webhook reachable", result.ok, detail)


async def check_model_loaded(
    expected_model: str,
    base_url: str = ollama_service.OLLAMA_LOCAL_BASE_URL,
    timeout: float = DEFAULT_CHECK_TIMEOUT,
) -> CheckResult:
    """Method: `GET /api/tags` against Ollama."""
    installed = await ollama_service.list_installed_models(base_url=base_url, timeout=timeout)
    names = ollama_service.installed_model_names(installed)
    if expected_model in names:
        return CheckResult("Model loaded", True, expected_model)
    detail = f"'{expected_model}' is not among the installed models: {sorted(names)}"
    return CheckResult("Model loaded", False, detail)


async def check_wireguard_panel(
    host: str,
    timeout: float = DEFAULT_CHECK_TIMEOUT,
    retry_timeout: float = DEFAULT_CHECK_RETRY_TIMEOUT,
) -> CheckResult:
    """Method: `GET http://<host>:51821` — never https, see §4.8."""
    return await _check_url_with_retry(
        "WireGuard panel",
        build_panel_url(host),
        timeout=timeout,
        retry_timeout=retry_timeout,
    )


def check_published_ports(
    compose_path: Path, wireguard_variant: str, timeout: float = compose.DEFAULT_TIMEOUT
) -> CheckResult:
    """Method: `docker compose ps` (never `ss`, see §4.2).

    In the `host` variant, WireGuard uses the host's network directly and does
    not appear in `Publishers` at all — there it is supplemented with
    `psutil`, which on native Linux does see the ports correctly… if the
    process is allowed to enumerate sockets. `doctor` does not elevate, and
    without privileges `psutil` denies the query: that used to translate into
    "every port is missing" and reported a healthy stack as broken. Now it
    says the check could not be performed.
    """
    statuses = {s.service: s for s in compose.ps(compose_path, timeout=timeout)}
    missing: list[str] = []
    unverifiable: list[str] = []

    nginx_status = statuses.get(NGINX_SERVICE)
    nginx_ports = set(nginx_status.published_ports) if nginx_status else set()
    for port in (80, 443):
        if port not in nginx_ports:
            missing.append(f"nginx:{port}")

    if wireguard_variant == WireguardVariant.PORTS.value:
        wg_status = statuses.get(WIREGUARD_SERVICE)
        wg_ports = set(wg_status.published_ports) if wg_status else set()
        for port in (51820, 51821):
            if port not in wg_ports:
                missing.append(f"wireguard:{port}")
    else:
        for status in detect_network.check_ports_psutil([(51820, "udp"), (51821, "tcp")]):
            if not status.inspectable:
                unverifiable.append(f"wireguard:{status.port}")
            elif not status.in_use:
                missing.append(f"wireguard:{status.port}")

    if missing:
        return CheckResult("Published ports", False, f"missing: {', '.join(missing)}")
    if unverifiable:
        return CheckResult(
            "Published ports",
            True,
            f"nginx OK; no privileges to check {', '.join(unverifiable)} "
            "(retry with sudo to verify them)",
        )
    return CheckResult("Published ports", True, "every expected port is published")


def check_ip_forwarding(wireguard_variant: str) -> CheckResult:
    """Method: read `net.ipv4.ip_forward` from `/proc/sys` (§6.1).

    It only means anything in the `host` variant: there the tunnel depends on
    the host kernel's own forwarding. It is a silent failure — the WireGuard
    handshake works and the client shows as connected, but not one packet gets
    through — so without this row there is no way to tell it apart from a
    client or router problem.
    """
    from axion_wizard.services import hostnet

    name = "IP forwarding (WireGuard)"
    if not hostnet.is_applicable(hostnet.current_os_name(), wireguard_variant):
        return CheckResult(name, True, "not applicable: Docker routes it in this variant")
    if hostnet.forwarding_is_active():
        return CheckResult(name, True, "net.ipv4.ip_forward = 1")
    return CheckResult(
        name,
        False,
        "net.ipv4.ip_forward is 0: the tunnel will establish but will not route "
        "anything. Fix it with: sudo sysctl -w net.ipv4.ip_forward=1 (made "
        f"persistent in {hostnet.SYSCTL_CONF_PATH})",
    )


# --- Mattermost's WebSocket -----------------------------------------------------
#
# Why this check exists: the symptom "the AI only answers when I press F5" is
# not the AI failing to answer — it is the AI answering and the message never
# reaching the browser. Mattermost pushes new messages over a WebSocket; on
# reload, the page re-fetches them over ordinary HTTP and they all appear at
# once. In other words: healthy HTTP + broken WebSocket.
#
# `doctor` used to check `GET https://<host>` and pass it, which is exactly
# the traffic that does work in that scenario. Diagnosing it took opening the
# browser devtools by hand. Here the handshake is performed for real and the
# two causes are told apart, since they call for opposite fixes.

MATTERMOST_WEBSOCKET_PATH = "/api/v4/websocket"
DEFAULT_WEBSOCKET_TIMEOUT = 10.0

WEBSOCKET_STALL_HINT = (
    "the connection opened but never completed the handshake: this is the signature "
    "of the WSL2 mirrored-networking TCP stall bug (moby/moby#48201). Typical symptom: "
    "messages only appear after pressing F5."
)
WEBSOCKET_REJECTED_HINT = (
    "the server rejected the handshake. Usually MM_SITEURL pointing at a different "
    "host from the one the browser uses, or nginx missing the Upgrade/Connection headers."
)


def _websocket_handshake_status(
    host: str, path: str = MATTERMOST_WEBSOCKET_PATH, timeout: float = DEFAULT_WEBSOCKET_TIMEOUT
) -> tuple[int | None, str]:
    """Perform the WebSocket handshake by hand and return `(code, detail)`.

    Done bare with `socket` + `ssl` rather than with `httpx`, because a
    `101 Switching Protocols` is a protocol change: h11 treats it as one and
    will not let the status line be read without fighting the library. Only
    the first line of the response is needed here, so the raw socket is both
    simpler and more reliable. The certificate is not verified: it is
    self-signed on purpose (§4.4).

    `code` is `None` when there was no response at all (timeout, dropped
    connection, failed TLS) — the case that gives away the mirrored-networking
    stall.
    """
    import base64
    import secrets as _secrets
    import ssl

    key = base64.b64encode(_secrets.token_bytes(16)).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Origin: https://{host}\r\n"
        "\r\n"
    ).encode("ascii")

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    try:
        with socket.create_connection((host, 443), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                tls.settimeout(timeout)
                tls.sendall(request)
                status_line = b""
                while b"\r\n" not in status_line:
                    chunk = tls.recv(256)
                    if not chunk:
                        break
                    status_line += chunk
    except (OSError, ssl.SSLError) as exc:
        return None, f"{type(exc).__name__}: {exc}"

    first_line = status_line.split(b"\r\n", 1)[0].decode("latin-1").strip()
    if not first_line:
        return None, "the server closed the connection without answering"

    parts = first_line.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return None, f"respuesta no reconocible: {first_line!r}"
    return int(parts[1]), first_line


def check_websocket(
    host: str, timeout: float = DEFAULT_WEBSOCKET_TIMEOUT
) -> CheckResult:
    """Method: a real WebSocket handshake against `wss://<host>/api/v4/websocket`.

    This is the check that separates "the AI does not answer" from "the AI
    answers and the browser does not find out until you reload".
    """
    name = "WebSocket Mattermost"
    status_code, detail = _websocket_handshake_status(host, timeout=timeout)

    if status_code is None:
        return CheckResult(name, False, f"{WEBSOCKET_STALL_HINT} ({detail})")
    if status_code == 101:
        return CheckResult(name, True, "handshake 101, real-time messaging operational")
    return CheckResult(name, False, f"HTTP {status_code} — {WEBSOCKET_REJECTED_HINT}")


# --- Orchestration -------------------------------------------------------------


async def run_all_checks(facts: DeploymentFacts) -> list[CheckResult]:
    return [
        check_containers_healthy(facts.compose_path),
        await check_https_responds(facts.host),
        # It sits immediately after the HTTPS check on purpose: same host,
        # same port, and seeing them consecutively is what makes the
        # diagnosis obvious when one passes and the other does not (see
        # `check_websocket`).
        check_websocket(facts.host),
        check_cert_has_san(facts.cert_path),
        check_webhook_reachable(facts.compose_path),
        await check_model_loaded(facts.ollama_model),
        await check_wireguard_panel(facts.host),
        check_published_ports(facts.compose_path, facts.wireguard_variant),
        check_ip_forwarding(facts.wireguard_variant),
    ]


def all_checks_passed(results: list[CheckResult]) -> bool:
    return all(r.ok for r in results)


def render_checks_table(results: list[CheckResult]) -> Table:
    table = ui.make_table("AXION verification")
    table.add_column("Check", style="axion.label")
    table.add_column("Resultado")
    table.add_column("Detalle", overflow="fold")
    for result in results:
        table.add_row(result.name, ui.status(result.ok), result.detail)
    return table


class VerifyStep(Step):
    """Step 9 as part of the `install` flow.

    It reuses exactly the same checks as `doctor` (§4.9), but starting from
    the configuration already in the context rather than rediscovering it from
    disk: in the middle of an `install` the artifacts have just been written
    and there is nothing to rebuild.
    """

    name = "verify"
    title = "Final verification"
    #: Not revalidated on resume: `verify()` is `run()`, so doing so would run
    #: all nine checks twice in a row. It is also the last step — there is
    #: nothing after it to protect.
    revalidate_on_resume = False

    def run(self) -> StepResult:
        if self.state.dry_run:
            console.print("[axion.info][dry-run][/] would run the final checks")
            return StepResult(name=self.name, ok=True, message="skipped by --dry-run")

        results = asyncio.run(run_all_checks(self._facts()))
        console.print(render_checks_table(results))

        failed = [r.name for r in results if not r.ok]
        if failed:
            # Nothing is raised: the stack is deployed and the user needs to
            # see the whole table. The orchestrator decides the exit code.
            return StepResult(
                name=self.name, ok=False, message=f"failed: {', '.join(failed)}"
            )
        return StepResult(name=self.name, ok=True, message=f"{len(results)} comprobaciones OK")

    def verify(self) -> StepResult:
        return self.run()

    def _facts(self) -> DeploymentFacts:
        config = self.context.require_config()
        project_dir = self.context.project_dir
        return DeploymentFacts(
            project_dir=project_dir,
            compose_path=project_dir / "docker-compose.yml",
            cert_path=project_dir / "nginx" / "certs" / "cert.crt",
            host=config.host,
            ollama_model=config.ollama_model,
            wireguard_variant=config.wireguard_variant.value,
        )
