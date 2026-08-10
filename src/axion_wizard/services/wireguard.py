"""Panel wg-easy: espera, autenticación, creación de cliente y QR (§4.8).

**Nota de implementación:** el contrato REST de wg-easy v14 (`/api/session`,
`/api/wireguard/client`) no está versionado ni documentado formalmente por
el proyecto — este módulo lo sigue de la mejor forma posible y valida la
forma de cada respuesta antes de usarla, en vez de asumir campos a ciegas,
para fallar con un `NetworkError`/`DeploymentError` accionable si el panel
responde de una forma inesperada.
"""

from __future__ import annotations

import io
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
import segno
from tenacity import AsyncRetrying, RetryError, retry_if_result, stop_after_delay, wait_exponential

from axion_wizard.errors import DeploymentError, NetworkError

DEFAULT_PANEL_PORT = 51821
DEFAULT_READY_TIMEOUT = 60.0
DEFAULT_HTTP_TIMEOUT = 10.0

PANEL_HTTPS_WARNING = (
    "El panel de WireGuard (wg-easy v14) sirve HTTP puro, no HTTPS. "
    "Abrirlo con https:// da ERR_SSL_PROTOCOL_ERROR — usar siempre http://."
)


def build_panel_url(host: str, port: int = DEFAULT_PANEL_PORT) -> str:
    """wg-easy v14 sirve HTTP puro: construir siempre con `http://` explícito
    (§4.8) — nunca `https://`, aunque el resto del stack use TLS."""
    return f"http://{host}:{port}"


async def wait_for_panel_ready(
    base_url: str,
    timeout: float = DEFAULT_READY_TIMEOUT,
    wait_min: float = 1.0,
    wait_max: float = 10.0,
) -> None:
    """Espera con backoff exponencial a que el panel responda algo (§4.8)."""

    async def _check() -> bool:
        async with httpx.AsyncClient(timeout=DEFAULT_HTTP_TIMEOUT) as client:
            try:
                response = await client.get(base_url)
            except httpx.HTTPError:
                return False
            return response.status_code < 500

    retryer = AsyncRetrying(
        stop=stop_after_delay(timeout),
        wait=wait_exponential(multiplier=wait_min, min=wait_min, max=wait_max),
        retry=retry_if_result(lambda ready: ready is False),
        reraise=False,
    )
    try:
        await retryer(_check)
    except RetryError as exc:
        raise NetworkError(
            what=f"El panel de WireGuard en {base_url} no respondió a tiempo",
            why=f"Se agotó el timeout de {timeout:g}s esperando una respuesta HTTP del panel.",
            steps=[
                "Verificar que el contenedor wireguard esté corriendo: "
                "docker compose ps wireguard",
                "Revisar sus logs: docker compose logs wireguard",
            ],
        ) from exc


@dataclass
class WireguardClient:
    id: str
    name: str
    config_text: str


class WireguardPanelClient:
    """Cliente HTTP de sesión única contra la API del panel wg-easy."""

    def __init__(self, base_url: str, timeout: float = DEFAULT_HTTP_TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> WireguardPanelClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def _send(
        self, method: Callable[..., Awaitable[httpx.Response]], path: str, **kwargs: Any
    ) -> httpx.Response:
        """Envuelve toda petición al panel para que un corte de red salga como
        `NetworkError` accionable y no como un `httpx.ConnectError` crudo.

        Va aquí y no en cada método porque el diagnóstico es idéntico en los
        tres: el contenedor se cayó, o la red entre wizard y panel dejó de
        funcionar a mitad del alta del cliente.
        """
        try:
            return await method(path, **kwargs)
        except httpx.HTTPError as exc:
            raise NetworkError(
                what=f"No se pudo contactar al panel de WireGuard en {self.base_url}",
                why=str(exc),
                steps=[
                    "Verificar que el contenedor wireguard esté corriendo: "
                    "docker compose ps wireguard",
                    "Revisar sus logs: docker compose logs wireguard",
                    "Reintentar el alta del cliente.",
                ],
            ) from exc

    async def login(self, password: str) -> None:
        response = await self._send(
            self._client.post, "/api/session", json={"password": password}
        )
        if response.status_code == 401:
            raise DeploymentError(
                what="El panel de WireGuard rechazó la contraseña",
                why="La contraseña no coincide con el hash configurado en wg.env (PASSWORD_HASH).",
                steps=["Verificar la contraseña del panel introducida en el paso 3."],
            )
        if response.status_code >= 400:
            raise DeploymentError(
                what=f"El panel de WireGuard devolvió {response.status_code} al autenticar",
                why=response.text.strip()[:500] or "Sin más detalle en el cuerpo de la respuesta.",
                steps=["Revisar los logs del contenedor wireguard."],
            )

    async def create_client(self, name: str) -> str:
        """Crea un cliente y devuelve su id. Valida la forma de la respuesta
        en vez de asumir un campo concreto, porque el contrato de wg-easy no
        está formalmente documentado."""
        response = await self._send(
            self._client.post, "/api/wireguard/client", json={"name": name}
        )
        if response.status_code >= 400:
            raise DeploymentError(
                what=f"El panel de WireGuard no pudo crear el cliente '{name}'",
                why=response.text.strip()[:500] or f"HTTP {response.status_code}",
                steps=["Revisar los logs del contenedor wireguard."],
            )
        client_id = _extract_client_id(response, name)
        if client_id is None:
            raise DeploymentError(
                what="La respuesta del panel al crear el cliente no trae un id reconocible",
                why=f"Cuerpo recibido: {response.text.strip()[:500]}",
                steps=[
                    "Reportar este error — el contrato de la API de wg-easy pudo haber cambiado."
                ],
            )
        return client_id

    async def get_client_configuration(self, client_id: str) -> str:
        response = await self._send(
            self._client.get, f"/api/wireguard/client/{client_id}/configuration"
        )
        if response.status_code >= 400:
            raise DeploymentError(
                what=f"No se pudo descargar la configuración del cliente {client_id}",
                why=response.text.strip()[:500] or f"HTTP {response.status_code}",
                steps=["Revisar los logs del contenedor wireguard."],
            )
        return response.text


def _extract_client_id(response: httpx.Response, name: str) -> str | None:
    try:
        data = response.json()
    except ValueError:
        return None

    if isinstance(data, dict):
        if isinstance(data.get("id"), str):
            return data["id"]
        client = data.get("client")
        if isinstance(client, dict) and isinstance(client.get("id"), str):
            return client["id"]
        clients = data.get("clients")
        if isinstance(clients, list):
            for entry in clients:
                if isinstance(entry, dict) and entry.get("name") == name and entry.get("id"):
                    return str(entry["id"])
    return None


def render_qr_terminal(data: str) -> str:
    """QR en caracteres de bloque Unicode, para mostrar en la propia
    terminal sin depender del navegador (§4.8)."""
    qr = segno.make(data)
    buffer = io.StringIO()
    qr.terminal(out=buffer, compact=True)
    return buffer.getvalue()


async def create_client_with_qr(panel: WireguardPanelClient, name: str) -> WireguardClient:
    client_id = await panel.create_client(name)
    config_text = await panel.get_client_configuration(client_id)
    return WireguardClient(id=client_id, name=name, config_text=config_text)
