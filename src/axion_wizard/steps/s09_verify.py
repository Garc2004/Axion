"""Paso 9 — Verificación final (§4.9).

El mismo conjunto de comprobaciones que ejecuta `axion-wizard doctor`. A
diferencia del paso de instalación, `doctor` no depende del estado
persistido de una corrida de `install` — reconstruye lo que necesita
(host, modelo, variante) leyendo directamente los artefactos ya escritos en
el `project_dir` (`docker-compose.yml`, `.env`, `wg.env`), así que puede
diagnosticar un stack desplegado por cualquier medio.
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values
from rich.table import Table
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_result,
    stop_after_delay,
    wait_exponential,
)

from axion_wizard import ui
from axion_wizard.config import WireguardVariant
from axion_wizard.console import console
from axion_wizard.detect import network as detect_network
from axion_wizard.errors import ConfigError
from axion_wizard.services import certs, compose
from axion_wizard.services import ollama as ollama_service
from axion_wizard.services.wireguard import build_panel_url
from axion_wizard.stack import (
    FASTAPI_SERVICE,
    MATTERMOST_SERVICE,
    NGINX_SERVICE,
    WIREGUARD_SERVICE,
)
from axion_wizard.steps.base import Step, StepResult

DEFAULT_CHECK_TIMEOUT = 10.0
#: Presupuesto total de reintento para las comprobaciones HTTP sensibles a
#: la latencia de acceso LAN bajo Docker Desktop/WSL2 (§6.5) — ver
#: `_check_url_with_retry`. Deliberadamente corto: `doctor` es un
#: diagnóstico rápido, no otra espera de despliegue.
DEFAULT_CHECK_RETRY_TIMEOUT = 20.0


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class DeploymentFacts:
    project_dir: Path
    compose_path: Path
    cert_path: Path
    host: str
    ollama_model: str
    wireguard_variant: str


# --- Descubrimiento de la configuración desplegada ----------------------------


def _host_from_site_url(site_url: str) -> str:
    """Extrae solo el host de un `MM_SITEURL`, sin esquema, puerto ni ruta.

    Mattermost admite despliegues en subruta (`https://ejemplo.com/mm`), así
    que quedarse con "todo lo que hay tras `://`" arrastraría la ruta y el
    puerto al identificador de host — que luego se concatena para formar,
    entre otras, la URL del panel WireGuard (`http://<host>:51821`).
    """
    site_url = site_url.strip()
    if not site_url:
        return ""
    # `urlsplit` solo reconoce la autoridad si hay esquema; si el valor viene
    # pelado (`192.168.1.50`) se lo añadimos para poder parsearlo igual.
    to_parse = site_url if "://" in site_url else f"//{site_url}"
    try:
        hostname = urlsplit(to_parse).hostname
    except ValueError:
        return site_url.split("://", 1)[-1].split("/", 1)[0].strip()
    return hostname or ""


def _detect_wireguard_variant_from_compose(compose_path: Path) -> str:
    """Deduce la variante del `network_mode` del servicio wireguard.

    Los errores de lectura/parseo se convierten en `ConfigError`: esta
    función está en el camino de *todo* `doctor`, y un `docker-compose.yml`
    corrupto o ilegible salía por el manejador genérico como
    `Error inesperado: …`, que es justo lo que §8 prohíbe.
    """
    yaml = YAML(typ="safe")
    try:
        data = yaml.load(compose_path.read_text(encoding="utf-8")) or {}
    except (YAMLError, OSError, UnicodeDecodeError) as exc:
        raise ConfigError(
            what=f"No se pudo leer {compose_path}",
            why=(
                "El archivo existe pero no se pudo parsear como YAML, así que no hay "
                f"forma de saber cómo está desplegado el stack: {exc}"
            ),
            steps=[
                "Revisar la sintaxis del docker-compose.yml.",
                "Restaurar una copia de seguridad (docker-compose.yml.*.bak) si la hay.",
                "O regenerarlo con `axion-wizard install`.",
            ],
        ) from exc

    if not isinstance(data, dict):
        raise ConfigError(
            what=f"{compose_path} no tiene la forma de un docker-compose.yml",
            why=(
                f"Se encontró {type(data).__name__} en la raíz donde Compose espera un "
                "mapping con claves como `services:`."
            ),
            steps=["Regenerar el archivo con `axion-wizard install`."],
        )

    services = data.get("services")
    wireguard_service = services.get(WIREGUARD_SERVICE) if isinstance(services, dict) else None
    if isinstance(wireguard_service, dict) and wireguard_service.get("network_mode") == "host":
        return WireguardVariant.HOST.value
    return WireguardVariant.PORTS.value


def discover_deployment(project_dir: Path) -> DeploymentFacts:
    """Reconstruye host/modelo/variante leyendo `docker-compose.yml`, `.env`
    y `wg.env` del `project_dir` — sin esto, `doctor` no puede correr contra
    un stack sin haber sido invocado primero por el `install` que lo creó."""
    compose_path = project_dir / "docker-compose.yml"
    if not compose_path.exists():
        raise ConfigError(
            what=f"No se encontró {compose_path}",
            why="`doctor` necesita un stack ya desplegado por `axion-wizard install`.",
            steps=[
                "Ejecutar `axion-wizard install` primero.",
                "O pasar el directorio correcto con --project-dir.",
            ],
        )

    env_values = dotenv_values(project_dir / ".env")
    wg_env_values = dotenv_values(project_dir / "wg.env")

    host = wg_env_values.get("WG_HOST") or _host_from_site_url(env_values.get("MM_SITEURL") or "")
    if not host:
        raise ConfigError(
            what="No se pudo determinar el host de acceso",
            why="Ni wg.env (WG_HOST) ni .env (MM_SITEURL) tienen un valor utilizable.",
            steps=["Verificar que .env y wg.env no estén corruptos o vacíos."],
        )

    ollama_model = env_values.get("OLLAMA_MODEL")
    if not ollama_model:
        raise ConfigError(
            what="No se pudo determinar el modelo de Ollama configurado",
            why="`.env` no tiene la variable OLLAMA_MODEL.",
            steps=["Verificar que .env no esté corrupto o incompleto."],
        )

    return DeploymentFacts(
        project_dir=project_dir,
        compose_path=compose_path,
        cert_path=project_dir / "nginx" / "certs" / "cert.crt",
        host=host,
        ollama_model=ollama_model,
        wireguard_variant=_detect_wireguard_variant_from_compose(compose_path),
    )


# --- Comprobaciones individuales (tabla de §4.9) -------------------------------


def check_containers_healthy(compose_path: Path) -> CheckResult:
    """Método: `docker compose ps --format json`."""
    statuses = compose.ps(compose_path)
    if not statuses:
        return CheckResult("Contenedores healthy", False, "no se pudo leer `docker compose ps`")
    unhealthy = [s for s in statuses if not s.is_running or not s.is_healthy_or_no_healthcheck]
    if unhealthy:
        names = ", ".join(s.service for s in unhealthy)
        return CheckResult("Contenedores healthy", False, f"no sanos: {names}")
    return CheckResult("Contenedores healthy", True, f"{len(statuses)} servicios OK")


async def _check_url_with_retry(
    name: str,
    url: str,
    *,
    verify: bool = True,
    timeout: float = DEFAULT_CHECK_TIMEOUT,
    retry_timeout: float = DEFAULT_CHECK_RETRY_TIMEOUT,
) -> CheckResult:
    """`GET url`, considerando OK cualquier respuesta que no sea 5xx.

    Reintenta con backoff corto en vez de un único intento: el acceso LAN
    bajo Docker Desktop/WSL2 puede tardar varios segundos en responder
    aunque funcione perfectamente (hallazgo real — un navegador o el propio
    `install`, que sí reintenta al crear el cliente de WireGuard, veían el
    servicio sin problema mientras un único intento de `doctor` lo marcaba
    FALLO). No enmascara un fallo real: si nunca responde, sigue fallando
    tras agotar `retry_timeout`.
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
        last_detail = f"HTTP {response.status_code} en {url}"
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
    """Método: `GET https://<host>` ignorando la verificación del cert autofirmado."""
    return await _check_url_with_retry(
        "HTTPS responde",
        f"https://{host}",
        verify=False,  # noqa: S501 - cert autofirmado a propósito, ver §4.4
        timeout=timeout,
        retry_timeout=retry_timeout,
    )


def check_cert_has_san(cert_path: Path) -> CheckResult:
    """Método: parsear con `cryptography`."""
    if not cert_path.exists():
        return CheckResult("Cert tiene SAN", False, f"{cert_path} no existe")
    try:
        san_entries = certs.verify_certificate_has_san(cert_path)
    except ConfigError as exc:
        return CheckResult("Cert tiene SAN", False, exc.what)
    return CheckResult("Cert tiene SAN", True, ", ".join(san_entries))


def check_webhook_reachable(
    compose_path: Path,
    mattermost_service: str = MATTERMOST_SERVICE,
    fastapi_service: str = FASTAPI_SERVICE,
    timeout: float = 15.0,
) -> CheckResult:
    """Método: `docker exec <mattermost>` → petición a `fastapi:8000/health`.

    `curl`, no `wget`: la imagen oficial de mattermost-team-edition no trae
    wget (mismo hallazgo que el healthcheck del propio servicio, §4.6) —
    esta comprobación fallaba siempre con "executable file not found in
    $PATH", indistinguible de un webhook realmente inalcanzable.
    """
    result = compose.exec_in_service(
        compose_path,
        mattermost_service,
        ["curl", "-fsS", f"http://{fastapi_service}:8000/health"],
        timeout=timeout,
    )
    detail = result.stdout.strip() or result.stderr.strip()
    return CheckResult("Webhook alcanzable", result.ok, detail)


async def check_model_loaded(
    expected_model: str,
    base_url: str = ollama_service.OLLAMA_LOCAL_BASE_URL,
    timeout: float = DEFAULT_CHECK_TIMEOUT,
) -> CheckResult:
    """Método: `GET /api/tags` en Ollama."""
    installed = await ollama_service.list_installed_models(base_url=base_url, timeout=timeout)
    names = ollama_service.installed_model_names(installed)
    if expected_model in names:
        return CheckResult("Modelo cargado", True, expected_model)
    detail = f"'{expected_model}' no está entre los instalados: {sorted(names)}"
    return CheckResult("Modelo cargado", False, detail)


async def check_wireguard_panel(
    host: str,
    timeout: float = DEFAULT_CHECK_TIMEOUT,
    retry_timeout: float = DEFAULT_CHECK_RETRY_TIMEOUT,
) -> CheckResult:
    """Método: `GET http://<host>:51821` — nunca https, ver §4.8."""
    return await _check_url_with_retry(
        "Panel WireGuard",
        build_panel_url(host),
        timeout=timeout,
        retry_timeout=retry_timeout,
    )


def check_published_ports(
    compose_path: Path, wireguard_variant: str, timeout: float = compose.DEFAULT_TIMEOUT
) -> CheckResult:
    """Método: `docker compose ps` (nunca `ss`, ver §4.2).

    En la variante `host`, WireGuard usa la red del host directamente y no
    aparece en absoluto en `Publishers` — ahí se complementa con `psutil`,
    que en Linux nativo sí ve los puertos correctamente… si el proceso puede
    enumerar sockets. `doctor` no eleva, y sin privilegios `psutil` deniega
    la consulta: antes eso se traducía en "faltan todos los puertos" y un
    stack sano se reportaba roto. Ahora se dice que no se pudo comprobar.
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
        return CheckResult("Puertos publicados", False, f"faltan: {', '.join(missing)}")
    if unverifiable:
        return CheckResult(
            "Puertos publicados",
            True,
            f"nginx OK; sin privilegios para comprobar {', '.join(unverifiable)} "
            "(reintentar con sudo para verificarlos)",
        )
    return CheckResult("Puertos publicados", True, "todos los puertos esperados están publicados")


def check_ip_forwarding(wireguard_variant: str) -> CheckResult:
    """Método: leer `net.ipv4.ip_forward` de `/proc/sys` (§6.1).

    Solo significa algo en la variante `host`: ahí el túnel depende del
    reenvío del kernel del propio host. Es un fallo mudo —el handshake de
    WireGuard funciona y el cliente aparece conectado, pero no pasa un solo
    paquete— así que sin esta fila no hay forma de distinguirlo de un
    problema del cliente o del router.
    """
    from axion_wizard.services import hostnet

    name = "Reenvío IP (WireGuard)"
    if not hostnet.is_applicable(hostnet.current_os_name(), wireguard_variant):
        return CheckResult(name, True, "no aplica: lo encamina Docker en esta variante")
    if hostnet.forwarding_is_active():
        return CheckResult(name, True, "net.ipv4.ip_forward = 1")
    return CheckResult(
        name,
        False,
        "net.ipv4.ip_forward está a 0: el túnel se establecerá pero no encaminará "
        f"nada. Arreglar con: sudo sysctl -w net.ipv4.ip_forward=1 (persistente en "
        f"{hostnet.SYSCTL_CONF_PATH})",
    )


# --- WebSocket de Mattermost ---------------------------------------------------
#
# Por qué existe esta comprobación: el síntoma "la IA solo responde cuando
# recargo con F5" no es que la IA no responda — es que responde y el mensaje
# nunca llega al navegador. Mattermost empuja los mensajes nuevos por
# WebSocket; al recargar, la página los re-pide por HTTP normal y aparecen de
# golpe. Es decir: HTTP sano + WebSocket roto.
#
# `doctor` comprobaba `GET https://<host>` y lo daba por bueno, que es
# exactamente el tráfico que sí funciona en ese escenario. Diagnosticarlo
# exigía abrir las devtools del navegador a mano. Aquí se hace el handshake
# de verdad y se distingue entre las dos causas, que piden arreglos opuestos.

MATTERMOST_WEBSOCKET_PATH = "/api/v4/websocket"
DEFAULT_WEBSOCKET_TIMEOUT = 10.0

WEBSOCKET_STALL_HINT = (
    "la conexión se abrió pero no completó el handshake: es la firma del bug de "
    "stalls TCP del mirrored networking de WSL2 (moby/moby#48201). Síntoma típico: "
    "los mensajes solo aparecen al recargar con F5."
)
WEBSOCKET_REJECTED_HINT = (
    "el servidor rechazó el handshake. Suele ser MM_SITEURL apuntando a un host "
    "distinto del que usa el navegador, o nginx sin las cabeceras Upgrade/Connection."
)


def _websocket_handshake_status(
    host: str, path: str = MATTERMOST_WEBSOCKET_PATH, timeout: float = DEFAULT_WEBSOCKET_TIMEOUT
) -> tuple[int | None, str]:
    """Hace el handshake WebSocket a mano y devuelve `(código, detalle)`.

    A pelo con `socket` + `ssl`, y no con `httpx`, porque un `101 Switching
    Protocols` es un cambio de protocolo: h11 lo trata como tal y no deja
    leer la línea de estado sin pelearse con la librería. Aquí solo hace
    falta la primera línea de la respuesta, así que el socket crudo es a la
    vez más simple y más fiable. El certificado no se verifica: es
    autofirmado a propósito (§4.4).

    `código` es `None` cuando no hubo respuesta (timeout, conexión cortada,
    TLS fallido) — el caso que delata el stall de mirrored networking.
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
        return None, "el servidor cerró la conexión sin responder"

    parts = first_line.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return None, f"respuesta no reconocible: {first_line!r}"
    return int(parts[1]), first_line


def check_websocket(
    host: str, timeout: float = DEFAULT_WEBSOCKET_TIMEOUT
) -> CheckResult:
    """Método: handshake WebSocket real contra `wss://<host>/api/v4/websocket`.

    Es la comprobación que separa "la IA no contesta" de "la IA contesta y el
    navegador no se entera hasta que recargas".
    """
    name = "WebSocket Mattermost"
    status_code, detail = _websocket_handshake_status(host, timeout=timeout)

    if status_code is None:
        return CheckResult(name, False, f"{WEBSOCKET_STALL_HINT} ({detail})")
    if status_code == 101:
        return CheckResult(name, True, "handshake 101, mensajería en tiempo real operativa")
    return CheckResult(name, False, f"HTTP {status_code} — {WEBSOCKET_REJECTED_HINT}")


# --- Orquestación --------------------------------------------------------------


async def run_all_checks(facts: DeploymentFacts) -> list[CheckResult]:
    return [
        check_containers_healthy(facts.compose_path),
        await check_https_responds(facts.host),
        # Va justo detrás del check de HTTPS a propósito: son el mismo host por
        # el mismo puerto, y verlos consecutivos es lo que hace obvio el
        # diagnóstico cuando uno pasa y el otro no (§ `check_websocket`).
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
    table = ui.make_table("Verificación de AXION")
    table.add_column("Comprobación", style="axion.label")
    table.add_column("Resultado")
    table.add_column("Detalle", overflow="fold")
    for result in results:
        table.add_row(result.name, ui.status(result.ok), result.detail)
    return table


class VerifyStep(Step):
    """Paso 9 como parte del flujo de `install`.

    Reutiliza exactamente las mismas comprobaciones que `doctor` (§4.9), pero
    partiendo de la configuración que ya está en el contexto en vez de
    redescubrirla del disco: en mitad de un `install` los artefactos acaban
    de escribirse y no hay nada que reconstruir.
    """

    name = "verify"
    title = "Verificación final"
    #: No se revalida al reanudar: `verify()` es `run()`, así que hacerlo
    #: ejecutaría las nueve comprobaciones dos veces seguidas. Es además el
    #: último paso — no hay nada después a lo que proteger.
    revalidate_on_resume = False

    def run(self) -> StepResult:
        if self.state.dry_run:
            console.print("[axion.info][dry-run][/] ejecutaría las verificaciones finales")
            return StepResult(name=self.name, ok=True, message="omitido por --dry-run")

        results = asyncio.run(run_all_checks(self._facts()))
        console.print(render_checks_table(results))

        failed = [r.name for r in results if not r.ok]
        if failed:
            # No se lanza: el stack está desplegado y el usuario necesita ver
            # la tabla completa. El orquestador decide el código de salida.
            return StepResult(
                name=self.name, ok=False, message=f"fallaron: {', '.join(failed)}"
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
