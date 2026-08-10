"""Panel wg-easy: espera, autenticación, creación de cliente y QR (§4.8).

**Nota de implementación:** el contrato REST de wg-easy no está versionado ni
documentado formalmente por el proyecto — este módulo lo sigue de la mejor
forma posible y valida la forma de cada respuesta antes de usarla, en vez de
asumir campos a ciegas, para fallar con un `NetworkError`/`DeploymentError`
accionable si el panel responde de una forma inesperada.

Está escrito contra la **v15** (verificado sobre la etiqueta `v15.3.0`), que
cambió el contrato entero respecto a la v14:

    v14                                  v15
    POST /api/session {password}         POST /api/session {username, password, remember}
    GET  /api/wireguard/client           GET  /api/client
    POST /api/wireguard/client           POST /api/client  -> devuelve el clientId
    GET  /api/wireguard/client/<id>/…    GET  /api/client/<id>/configuration

El cambio más útil es el último: la v14 respondía `{"success": true}` sin el
objeto creado, así que había que listar antes y después y comparar ids para
averiguar cuál era el nuevo. La v15 devuelve `clientId` directamente y todo
ese baile desapareció.
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
    "El panel de WireGuard sirve HTTP puro, no HTTPS (arranca con INSECURE=true). "
    "Abrirlo con https:// da ERR_SSL_PROTOCOL_ERROR — usar siempre http://."
)


def build_panel_url(host: str, port: int = DEFAULT_PANEL_PORT) -> str:
    """El panel sirve HTTP puro: construir siempre con `http://` explícito
    (§4.8) — nunca `https://`, aunque el resto del stack use TLS.

    En la v15 esto es además una consecuencia declarada: el `wg.env` que
    genera el wizard pone `INSECURE=true`, sin lo cual el panel directamente
    se niega a responder por HTTP.
    """
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

    async def login(self, username: str, password: str) -> None:
        """Autentica contra el panel v15.

        Tres detalles del contrato que no se pueden omitir:

        - `username` es obligatorio. La v14 no tenía usuarios.
        - `remember` también, aunque parezca opcional: su esquema zod lo
          declara `z.boolean()` sin `.optional()`, así que omitirlo devuelve
          un 400 de validación que no menciona el campo que falta.
        - Un 200 **no** significa que se haya entrado. Con 2FA activo el
          panel responde 200 con `{"status": "TOTP_REQUIRED"}` y sin sesión;
          darlo por bueno dejaba al wizard llamando a `/api/client` sin
          autenticar y fallando después con un 401 que parecía otra cosa.
        """
        response = await self._send(
            self._client.post,
            "/api/session",
            json={"username": username, "password": password, "remember": False},
        )
        if response.status_code == 401:
            raise DeploymentError(
                what="El panel de WireGuard rechazó las credenciales",
                why=(
                    f"El usuario {username!r} y su contraseña no coinciden con los que "
                    "wg-easy tiene guardados."
                ),
                steps=[
                    "Verificar el usuario y la contraseña del panel del paso 3 "
                    "(INIT_USERNAME / INIT_PASSWORD en wg.env).",
                    "Recordar que INIT_* solo se aplica en el primer arranque: si el "
                    "volumen ya existía, valen las credenciales de entonces.",
                ],
            )
        if response.status_code >= 400:
            raise DeploymentError(
                what=f"El panel de WireGuard devolvió {response.status_code} al autenticar",
                why=response.text.strip()[:500] or "Sin más detalle en el cuerpo de la respuesta.",
                steps=["Revisar los logs del contenedor wireguard."],
            )

        status = self._json_status(response)
        if status == "TOTP_REQUIRED":
            raise DeploymentError(
                what="El panel de WireGuard pide un segundo factor",
                why=(
                    "La cuenta del panel tiene 2FA activo, y el wizard no tiene de "
                    "dónde sacar el código."
                ),
                steps=[
                    f"Crear el cliente desde el panel: {self.base_url}",
                    "O desactivar temporalmente el 2FA de esa cuenta y reintentar.",
                ],
            )
        if status == "INVALID_TOTP_CODE":
            raise DeploymentError(
                what="El panel de WireGuard rechazó el segundo factor",
                why="wg-easy respondió INVALID_TOTP_CODE al autenticar.",
                steps=[f"Crear el cliente desde el panel: {self.base_url}"],
            )

    @staticmethod
    def _json_status(response: httpx.Response) -> str | None:
        """El campo `status` de una respuesta del panel, si lo trae.

        Un cuerpo que no sea JSON no es motivo para abortar: solo significa
        que no hay estado que interpretar.
        """
        try:
            data = response.json()
        except ValueError:
            return None
        if isinstance(data, dict) and isinstance(data.get("status"), str):
            return data["status"]
        return None

    async def list_clients(self) -> list[dict[str, Any]]:
        """Todos los clientes ya dados de alta en el panel."""
        response = await self._send(self._client.get, "/api/client")
        if response.status_code >= 400:
            raise DeploymentError(
                what="No se pudo listar los clientes del panel de WireGuard",
                why=response.text.strip()[:500] or f"HTTP {response.status_code}",
                steps=["Revisar los logs del contenedor wireguard."],
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise DeploymentError(
                what="La lista de clientes del panel no es JSON válido",
                why=response.text.strip()[:500],
                steps=[
                    "Reportar este error — el contrato de la API de wg-easy pudo haber cambiado."
                ],
            ) from exc
        if not isinstance(data, list):
            raise DeploymentError(
                what="La lista de clientes del panel no tiene la forma esperada",
                why=f"Se esperaba un array JSON; llegó {type(data).__name__}.",
                steps=[
                    "Reportar este error — el contrato de la API de wg-easy pudo haber cambiado."
                ],
            )
        return [entry for entry in data if isinstance(entry, dict)]

    async def create_client(self, name: str) -> str:
        """Crea un cliente y devuelve su id.

        La v15 responde `{"success": true, "clientId": ...}`, así que el id
        sale de la propia respuesta. La v14 no lo devolvía —solo
        `{"success": true}`— y obligaba a listar antes y después y comparar
        ids para deducir cuál era el nuevo, con la ambigüedad extra de que
        wg-easy nunca ha exigido nombres únicos. Ese rodeo entero
        desapareció con el cambio de versión.
        """
        response = await self._send(self._client.post, "/api/client", json={"name": name})
        if response.status_code >= 400:
            raise DeploymentError(
                what=f"El panel de WireGuard no pudo crear el cliente '{name}'",
                why=response.text.strip()[:500] or f"HTTP {response.status_code}",
                steps=["Revisar los logs del contenedor wireguard."],
            )

        client_id = _client_id_from_creation(response)
        if client_id is None:
            raise DeploymentError(
                what="El panel no devolvió el id del cliente recién creado",
                why=(
                    f"Se esperaba `clientId` en la respuesta a la creación de '{name}'; "
                    f"llegó: {response.text.strip()[:200] or '(cuerpo vacío)'}"
                ),
                steps=[
                    f"Comprobar en el panel si '{name}' se creó igual, en {self.base_url}",
                    "Reportar este error — el contrato de la API de wg-easy pudo haber cambiado.",
                ],
            )
        return client_id

    async def get_client_configuration(self, client_id: str) -> str:
        response = await self._send(
            self._client.get, f"/api/client/{client_id}/configuration"
        )
        if response.status_code >= 400:
            raise DeploymentError(
                what=f"No se pudo descargar la configuración del cliente {client_id}",
                why=response.text.strip()[:500] or f"HTTP {response.status_code}",
                steps=["Revisar los logs del contenedor wireguard."],
            )
        return response.text


def _client_id_from_creation(response: httpx.Response) -> str | None:
    """El `clientId` de la respuesta a `POST /api/client`, si viene.

    Se acepta un id numérico además de una cadena: el esquema del panel lo
    declara como el identificador de la fila, y confiar en que siempre
    llegue serializado como texto es exactamente el tipo de suposición que
    este módulo evita en el resto de respuestas.
    """
    try:
        data = response.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    client_id = data.get("clientId")
    if isinstance(client_id, str) and client_id:
        return client_id
    if isinstance(client_id, int):
        return str(client_id)
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
