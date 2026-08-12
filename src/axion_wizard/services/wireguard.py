"""The wg-easy panel: waiting, authentication, client creation and QR (§4.8).

**Implementation note:** wg-easy's REST contract is neither versioned nor
formally documented by the project — this module follows it as closely as it
can and validates the shape of every response before using it, rather than
assuming fields blindly, so that an unexpected answer surfaces as an
actionable `NetworkError`/`DeploymentError`.

It is written against **v15** (verified against the `v15.3.0` tag), which
changed the entire contract from v14:

    v14                                  v15
    POST /api/session {password}         POST /api/session {username, password, remember}
    GET  /api/wireguard/client           GET  /api/client
    POST /api/wireguard/client           POST /api/client  -> returns the clientId
    GET  /api/wireguard/client/<id>/…    GET  /api/client/<id>/configuration

The last change is the most useful: v14 answered `{"success": true}` without
the created object, so the only way to learn the new id was to list before
and after and compare. v15 returns `clientId` directly and that whole dance
went away.
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
    "The WireGuard panel serves plain HTTP, not HTTPS (it starts with INSECURE=true). "
    "Opening it with https:// gives ERR_SSL_PROTOCOL_ERROR — always use http://."
)

#: Printed after a client's QR, in both the install step and `wireguard
#: add-client`. Used to be a single line — "scan the QR with the WireGuard
#: app, or import the configuration manually from the panel" — that named no
#: app store, no menu, no file, leaving anyone without a WireGuard client
#: already installed to guess the rest.
CLIENT_APP_SETUP_STEPS = (
    "  1. Install WireGuard — wireguard.com/install, or your phone's app store.\n"
    "  2. Mobile: tap + → Scan from QR code, and scan the code above.\n"
    "     Desktop: + → Import tunnel(s) from file, using the .conf downloaded "
    "from the panel — or paste the text above into a new empty tunnel.\n"
    "  3. Turn the tunnel on — the toggle next to its name."
)


def build_panel_url(host: str, port: int = DEFAULT_PANEL_PORT) -> str:
    """The panel serves plain HTTP: always build with an explicit `http://`
    (§4.8) — never `https://`, even though the rest of the stack uses TLS.

    On v15 this is a declared consequence too: the `wg.env` the wizard
    generates sets `INSECURE=true`, without which the panel flatly refuses to
    answer over HTTP.
    """
    return f"http://{host}:{port}"


async def wait_for_panel_ready(
    base_url: str,
    timeout: float = DEFAULT_READY_TIMEOUT,
    wait_min: float = 1.0,
    wait_max: float = 10.0,
) -> None:
    """Wait with exponential backoff for the panel to answer anything (§4.8)."""

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
            what=f"The WireGuard panel at {base_url} did not answer in time",
            why=f"The {timeout:g}s timeout expired waiting for an HTTP response from the panel.",
            steps=[
                "Check the wireguard container is running: docker compose ps wireguard",
                "Read its logs: docker compose logs wireguard",
            ],
        ) from exc


@dataclass
class WireguardClient:
    id: str
    name: str
    config_text: str


class WireguardPanelClient:
    """A single-session HTTP client against the wg-easy panel API."""

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
        """Wrap every request to the panel so a network drop surfaces as an
        actionable `NetworkError` rather than a raw `httpx.ConnectError`.

        It lives here and not in each method because the diagnosis is
        identical in all three: the container went down, or the network
        between wizard and panel stopped working mid-enrolment.
        """
        try:
            return await method(path, **kwargs)
        except httpx.HTTPError as exc:
            raise NetworkError(
                what=f"Could not reach the WireGuard panel at {self.base_url}",
                why=str(exc),
                steps=[
                    "Check the wireguard container is running: docker compose ps wireguard",
                    "Read its logs: docker compose logs wireguard",
                    "Retry enrolling the client.",
                ],
            ) from exc

    async def login(self, username: str, password: str) -> None:
        """Authenticate against the v15 panel.

        Three details of the contract that cannot be skipped:

        - `username` is mandatory. v14 had no users at all.
        - So is `remember`, however optional it looks: its zod schema declares
          it `z.boolean()` without `.optional()`, so omitting it returns a 400
          validation error that never mentions the missing field.
        - A 200 does **not** mean you are in. With 2FA enabled the panel
          answers 200 with `{"status": "TOTP_REQUIRED"}` and no session;
          taking that at face value left the wizard calling `/api/client`
          unauthenticated and failing later with a 401 that looked like
          something else entirely.
        """
        response = await self._send(
            self._client.post,
            "/api/session",
            json={"username": username, "password": password, "remember": False},
        )
        if response.status_code == 401:
            raise DeploymentError(
                what="The WireGuard panel rejected the credentials",
                why=(
                    f"User {username!r} and its password do not match what wg-easy has "
                    "stored."
                ),
                steps=[
                    "Check the panel username and password from step 3 "
                    "(INIT_USERNAME / INIT_PASSWORD in wg.env).",
                    "Remember INIT_* only applies on first boot: if the volume already "
                    "existed, the credentials from back then are the ones that count.",
                ],
            )
        if response.status_code >= 400:
            raise DeploymentError(
                what=f"The WireGuard panel returned {response.status_code} on authentication",
                why=response.text.strip()[:500] or "No further detail in the response body.",
                steps=["Read the wireguard container's logs."],
            )

        status = self._json_status(response)
        if status == "TOTP_REQUIRED":
            raise DeploymentError(
                what="The WireGuard panel is asking for a second factor",
                why=(
                    "The panel account has 2FA enabled, and the wizard has nowhere to "
                    "get the code from."
                ),
                steps=[
                    f"Create the client from the panel instead: {self.base_url}",
                    "Or temporarily disable 2FA on that account and retry.",
                ],
            )
        if status == "INVALID_TOTP_CODE":
            raise DeploymentError(
                what="The WireGuard panel rejected the second factor",
                why="wg-easy answered INVALID_TOTP_CODE on authentication.",
                steps=[f"Create the client from the panel instead: {self.base_url}"],
            )

    @staticmethod
    def _json_status(response: httpx.Response) -> str | None:
        """The `status` field of a panel response, if it carries one.

        A non-JSON body is no reason to abort: it only means there is no
        status to interpret.
        """
        try:
            data = response.json()
        except ValueError:
            return None
        if isinstance(data, dict) and isinstance(data.get("status"), str):
            return data["status"]
        return None

    async def list_clients(self) -> list[dict[str, Any]]:
        """Every client already enrolled in the panel."""
        response = await self._send(self._client.get, "/api/client")
        if response.status_code >= 400:
            raise DeploymentError(
                what="Could not list the WireGuard panel's clients",
                why=response.text.strip()[:500] or f"HTTP {response.status_code}",
                steps=["Read the wireguard container's logs."],
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise DeploymentError(
                what="The panel's client list is not valid JSON",
                why=response.text.strip()[:500],
                steps=[
                    "Report this error — wg-easy's API contract may have changed."
                ],
            ) from exc
        if not isinstance(data, list):
            raise DeploymentError(
                what="The panel's client list is not shaped as expected",
                why=f"A JSON array was expected; {type(data).__name__} arrived.",
                steps=[
                    "Report this error — wg-easy's API contract may have changed."
                ],
            )
        return [entry for entry in data if isinstance(entry, dict)]

    async def create_client(self, name: str) -> str:
        """Create a client and return its id.

        v15 answers `{"success": true, "clientId": …}`, so the id comes
        straight out of the response. v14 did not return it — only
        `{"success": true}` — and forced a list-before, list-after comparison
        to deduce which one was new, with the added ambiguity that wg-easy has
        never required unique names. That entire detour went away with the
        version change.

        `expiresAt` has to be sent, explicitly `None` — the same trap
        `login`'s `remember` already warned about. The panel's zod schema
        declares it `.nullable()` without `.optional()`: that accepts `null`
        or a string, but not the key being absent, so omitting it entirely
        (as if it truly were optional) got every client creation rejected
        with a 400 whose body says nothing about which field was the problem.
        """
        response = await self._send(
            self._client.post, "/api/client", json={"name": name, "expiresAt": None}
        )
        if response.status_code >= 400:
            raise DeploymentError(
                what=f"The WireGuard panel could not create client '{name}'",
                why=response.text.strip()[:500] or f"HTTP {response.status_code}",
                steps=["Read the wireguard container's logs."],
            )

        client_id = _client_id_from_creation(response)
        if client_id is None:
            raise DeploymentError(
                what="The panel did not return the id of the client it just created",
                why=(
                    f"`clientId` was expected in the response to creating '{name}'; "
                    f"what arrived was: {response.text.strip()[:200] or '(empty body)'}"
                ),
                steps=[
                    f"Check in the panel whether '{name}' was created anyway, at {self.base_url}",
                    "Report this error — wg-easy's API contract may have changed.",
                ],
            )
        return client_id

    async def get_client_configuration(self, client_id: str) -> str:
        response = await self._send(
            self._client.get, f"/api/client/{client_id}/configuration"
        )
        if response.status_code >= 400:
            raise DeploymentError(
                what=f"Could not download the configuration for client {client_id}",
                why=response.text.strip()[:500] or f"HTTP {response.status_code}",
                steps=["Read the wireguard container's logs."],
            )
        return response.text


def _client_id_from_creation(response: httpx.Response) -> str | None:
    """The `clientId` from the response to `POST /api/client`, if present.

    A numeric id is accepted as well as a string: the panel's schema declares
    it as the row identifier, and trusting that it always arrives serialised
    as text is exactly the kind of assumption this module avoids everywhere
    else.
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
    """A QR code in Unicode block characters, to display in the terminal
    itself without depending on a browser (§4.8)."""
    qr = segno.make(data)
    buffer = io.StringIO()
    qr.terminal(out=buffer, compact=True)
    return buffer.getvalue()


async def create_client_with_qr(panel: WireguardPanelClient, name: str) -> WireguardClient:
    client_id = await panel.create_client(name)
    config_text = await panel.get_client_configuration(client_id)
    return WireguardClient(id=client_id, name=name, config_text=config_text)
