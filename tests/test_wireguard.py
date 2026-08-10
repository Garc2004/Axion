import asyncio

import httpx
import pytest

from axion_wizard.errors import DeploymentError, NetworkError
from axion_wizard.services import wireguard as wg

# --- build_panel_url -----------------------------------------------------------


def test_build_panel_url_uses_http_explicitly() -> None:
    assert wg.build_panel_url("192.168.1.50") == "http://192.168.1.50:51821"


def test_build_panel_url_custom_port() -> None:
    assert wg.build_panel_url("axion.example.com", port=8080) == "http://axion.example.com:8080"


def test_panel_https_warning_mentions_http() -> None:
    assert "http://" in wg.PANEL_HTTPS_WARNING
    assert "ERR_SSL_PROTOCOL_ERROR" in wg.PANEL_HTTPS_WARNING


# --- wait_for_panel_ready --------------------------------------------------------


def _mock_get_client(mocker, responses=None, side_effect=None):
    mock_client = mocker.AsyncMock()
    if side_effect is not None:
        mock_client.get = mocker.AsyncMock(side_effect=side_effect)
    else:
        mock_client.get = mocker.AsyncMock(side_effect=list(responses))
    mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = mocker.AsyncMock(return_value=False)
    return mock_client


def test_wait_for_panel_ready_succeeds_immediately(mocker) -> None:
    ok_response = httpx.Response(200)
    mock_client = _mock_get_client(mocker, responses=[ok_response])
    mocker.patch("axion_wizard.services.wireguard.httpx.AsyncClient", return_value=mock_client)

    asyncio.run(
        wg.wait_for_panel_ready("http://x:51821", timeout=2.0, wait_min=0.01, wait_max=0.02)
    )


def test_wait_for_panel_ready_retries_then_succeeds(mocker) -> None:
    call_count = {"n": 0}

    async def flaky_get(url):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise httpx.ConnectError("refused")
        return httpx.Response(200)

    mock_client = mocker.AsyncMock()
    mock_client.get = flaky_get
    mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = mocker.AsyncMock(return_value=False)
    mocker.patch("axion_wizard.services.wireguard.httpx.AsyncClient", return_value=mock_client)

    asyncio.run(
        wg.wait_for_panel_ready("http://x:51821", timeout=5.0, wait_min=0.01, wait_max=0.02)
    )
    assert call_count["n"] == 3


def test_wait_for_panel_ready_raises_network_error_on_timeout(mocker) -> None:
    mock_client = _mock_get_client(mocker, side_effect=httpx.ConnectError("refused"))
    mocker.patch("axion_wizard.services.wireguard.httpx.AsyncClient", return_value=mock_client)

    with pytest.raises(NetworkError, match="no respondió"):
        asyncio.run(
            wg.wait_for_panel_ready("http://x:51821", timeout=0.05, wait_min=0.01, wait_max=0.02)
        )


# --- _pick_new_client_id ----------------------------------------------------------


def test_pick_new_client_id_single_candidate() -> None:
    assert wg._pick_new_client_id([{"id": "abc", "name": "my-phone"}], "my-phone") == "abc"


def test_pick_new_client_id_no_candidates_returns_none() -> None:
    assert wg._pick_new_client_id([], "my-phone") is None


def test_pick_new_client_id_multiple_candidates_prefers_matching_name() -> None:
    """Concurrencia real: otra alta contra el mismo panel entre el listado
    de "antes" y el de "después"."""
    candidates = [
        {"id": "other", "name": "otro-dispositivo", "createdAt": "2026-01-01T00:00:00.000Z"},
        {"id": "wanted", "name": "my-phone", "createdAt": "2026-01-01T00:00:01.000Z"},
    ]
    assert wg._pick_new_client_id(candidates, "my-phone") == "wanted"


def test_pick_new_client_id_ties_on_name_prefer_newest() -> None:
    """wg-easy no exige nombres únicos (§ investigación de fuente real):
    dos clientes nuevos pueden compartir nombre, y ahí gana el más reciente."""
    candidates = [
        {"id": "older", "name": "my-phone", "createdAt": "2026-01-01T00:00:00.000Z"},
        {"id": "newer", "name": "my-phone", "createdAt": "2026-01-01T00:00:01.000Z"},
    ]
    assert wg._pick_new_client_id(candidates, "my-phone") == "newer"


# --- WireguardPanelClient.list_clients ---------------------------------------------


def test_list_clients_returns_bare_array(mocker) -> None:
    """wg-easy v14 responde un array JSON suelto, sin envolver (confirmado
    contra su código fuente), a diferencia de otros endpoints del panel."""
    mock_inner = _inner_client(
        mocker,
        get=mocker.AsyncMock(
            return_value=httpx.Response(200, json=[{"id": "abc", "name": "my-phone"}])
        ),
    )
    panel = _client_with_mocked_httpx(mocker, mock_inner)
    clients = asyncio.run(panel.list_clients())
    assert clients == [{"id": "abc", "name": "my-phone"}]


def test_list_clients_rejects_a_non_array_body(mocker) -> None:
    mock_inner = _inner_client(
        mocker, get=mocker.AsyncMock(return_value=httpx.Response(200, json={"clients": []}))
    )
    panel = _client_with_mocked_httpx(mocker, mock_inner)
    with pytest.raises(DeploymentError, match="forma esperada"):
        asyncio.run(panel.list_clients())


def test_list_clients_rejects_non_json_body(mocker) -> None:
    mock_inner = _inner_client(
        mocker, get=mocker.AsyncMock(return_value=httpx.Response(200, text="not json"))
    )
    panel = _client_with_mocked_httpx(mocker, mock_inner)
    with pytest.raises(DeploymentError, match="JSON válido"):
        asyncio.run(panel.list_clients())


def test_list_clients_http_error_raises_deployment_error(mocker) -> None:
    mock_inner = _inner_client(
        mocker, get=mocker.AsyncMock(return_value=httpx.Response(500, text="boom"))
    )
    panel = _client_with_mocked_httpx(mocker, mock_inner)
    with pytest.raises(DeploymentError, match="No se pudo listar"):
        asyncio.run(panel.list_clients())


# --- WireguardPanelClient.login ---------------------------------------------------


def _client_with_mocked_httpx(mocker, mock_inner_client):
    mocker.patch(
        "axion_wizard.services.wireguard.httpx.AsyncClient", return_value=mock_inner_client
    )
    return wg.WireguardPanelClient("http://192.168.1.50:51821")


def _inner_client(mocker, **method_mocks):
    mock_client = mocker.AsyncMock()
    for method_name, mock in method_mocks.items():
        setattr(mock_client, method_name, mock)
    return mock_client


def test_login_success(mocker) -> None:
    mock_inner = _inner_client(mocker, post=mocker.AsyncMock(return_value=httpx.Response(200)))
    panel = _client_with_mocked_httpx(mocker, mock_inner)
    asyncio.run(panel.login("correct-password"))  # no debe lanzar


def test_login_wrong_password_raises_deployment_error(mocker) -> None:
    mock_inner = _inner_client(mocker, post=mocker.AsyncMock(return_value=httpx.Response(401)))
    panel = _client_with_mocked_httpx(mocker, mock_inner)
    with pytest.raises(DeploymentError, match="rechazó"):
        asyncio.run(panel.login("wrong-password"))


def test_login_server_error_raises_deployment_error(mocker) -> None:
    mock_inner = _inner_client(
        mocker, post=mocker.AsyncMock(return_value=httpx.Response(500, text="boom"))
    )
    panel = _client_with_mocked_httpx(mocker, mock_inner)
    with pytest.raises(DeploymentError):
        asyncio.run(panel.login("x"))


def test_login_connection_error_raises_network_error(mocker) -> None:
    mock_inner = _inner_client(
        mocker, post=mocker.AsyncMock(side_effect=httpx.ConnectError("refused"))
    )
    panel = _client_with_mocked_httpx(mocker, mock_inner)
    with pytest.raises(NetworkError):
        asyncio.run(panel.login("x"))


# --- WireguardPanelClient.create_client / get_client_configuration ---------------


def test_create_client_success(mocker) -> None:
    """wg-easy v14 responde `{"success": true}` a la creación, nunca el
    cliente creado (confirmado contra su código fuente real) — el id sale
    de comparar el listado de antes y de después, no de la respuesta del
    POST."""
    mock_inner = _inner_client(
        mocker,
        get=mocker.AsyncMock(
            side_effect=[
                httpx.Response(200, json=[]),
                httpx.Response(200, json=[{"id": "abc", "name": "my-phone"}]),
            ]
        ),
        post=mocker.AsyncMock(return_value=httpx.Response(200, json={"success": True})),
    )
    panel = _client_with_mocked_httpx(mocker, mock_inner)
    client_id = asyncio.run(panel.create_client("my-phone"))
    assert client_id == "abc"


def test_create_client_only_considers_ids_absent_from_the_before_listing(mocker) -> None:
    """Un cliente que ya existía antes de esta alta no debe confundirse con
    el nuevo, aunque siga apareciendo en el listado de después."""
    mock_inner = _inner_client(
        mocker,
        get=mocker.AsyncMock(
            side_effect=[
                httpx.Response(200, json=[{"id": "ya-existia", "name": "otro"}]),
                httpx.Response(
                    200,
                    json=[
                        {"id": "ya-existia", "name": "otro"},
                        {"id": "nuevo", "name": "my-phone"},
                    ],
                ),
            ]
        ),
        post=mocker.AsyncMock(return_value=httpx.Response(200, json={"success": True})),
    )
    panel = _client_with_mocked_httpx(mocker, mock_inner)
    assert asyncio.run(panel.create_client("my-phone")) == "nuevo"


def test_create_client_http_error_raises_deployment_error(mocker) -> None:
    mock_inner = _inner_client(
        mocker,
        get=mocker.AsyncMock(return_value=httpx.Response(200, json=[])),
        post=mocker.AsyncMock(return_value=httpx.Response(400, text="bad name")),
    )
    panel = _client_with_mocked_httpx(mocker, mock_inner)
    with pytest.raises(DeploymentError, match="no pudo crear"):
        asyncio.run(panel.create_client("my-phone"))


def test_create_client_when_the_listing_shows_no_new_client_raises_deployment_error(
    mocker,
) -> None:
    """Defensivo: si tras un POST que devolvió éxito el listado no cambia,
    algo no encaja con lo que la API documenta — mejor fallar accionable
    que devolver un id equivocado."""
    same_listing = httpx.Response(200, json=[{"id": "sin-cambios", "name": "otro"}])
    mock_inner = _inner_client(
        mocker,
        get=mocker.AsyncMock(side_effect=[same_listing, same_listing]),
        post=mocker.AsyncMock(return_value=httpx.Response(200, json={"success": True})),
    )
    panel = _client_with_mocked_httpx(mocker, mock_inner)
    with pytest.raises(DeploymentError, match="No se pudo identificar"):
        asyncio.run(panel.create_client("my-phone"))


def test_get_client_configuration_success(mocker) -> None:
    conf_text = "[Interface]\nPrivateKey = xxx\n"
    mock_inner = _inner_client(
        mocker, get=mocker.AsyncMock(return_value=httpx.Response(200, text=conf_text))
    )
    panel = _client_with_mocked_httpx(mocker, mock_inner)
    result = asyncio.run(panel.get_client_configuration("abc"))
    assert result == conf_text


def test_get_client_configuration_failure_raises_deployment_error(mocker) -> None:
    mock_inner = _inner_client(
        mocker, get=mocker.AsyncMock(return_value=httpx.Response(404, text="not found"))
    )
    panel = _client_with_mocked_httpx(mocker, mock_inner)
    with pytest.raises(DeploymentError):
        asyncio.run(panel.get_client_configuration("abc"))


def test_create_client_connection_error_raises_network_error(mocker) -> None:
    """El panel puede caerse *después* del login, a mitad del alta: eso debe
    salir como NetworkError accionable, no como un httpx.ConnectError crudo
    convertido en 'Error inesperado'."""
    mock_inner = _inner_client(
        mocker,
        get=mocker.AsyncMock(return_value=httpx.Response(200, json=[])),
        post=mocker.AsyncMock(side_effect=httpx.ConnectError("refused")),
    )
    panel = _client_with_mocked_httpx(mocker, mock_inner)
    with pytest.raises(NetworkError, match="No se pudo contactar"):
        asyncio.run(panel.create_client("my-phone"))


def test_get_client_configuration_connection_error_raises_network_error(mocker) -> None:
    mock_inner = _inner_client(
        mocker, get=mocker.AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
    )
    panel = _client_with_mocked_httpx(mocker, mock_inner)
    with pytest.raises(NetworkError):
        asyncio.run(panel.get_client_configuration("abc"))


def test_panel_client_async_context_manager_closes(mocker) -> None:
    mock_inner = _inner_client(mocker, aclose=mocker.AsyncMock())
    mocker.patch(
        "axion_wizard.services.wireguard.httpx.AsyncClient", return_value=mock_inner
    )

    async def run() -> None:
        async with wg.WireguardPanelClient("http://x:51821"):
            pass

    asyncio.run(run())
    mock_inner.aclose.assert_called_once()


# --- render_qr_terminal ------------------------------------------------------------


def test_render_qr_terminal_returns_multiline_unicode_block_art() -> None:
    text = wg.render_qr_terminal("[Interface]\nPrivateKey = xxx\n")
    assert isinstance(text, str)
    assert len(text.splitlines()) > 1
    assert chr(27) not in text  # solo bloques Unicode, sin secuencias ANSI


def test_render_qr_terminal_different_input_different_output() -> None:
    a = wg.render_qr_terminal("config-a")
    b = wg.render_qr_terminal("config-b")
    assert a != b


# --- create_client_with_qr ----------------------------------------------------------


def test_create_client_with_qr_orchestration(mocker) -> None:
    panel = mocker.AsyncMock()
    panel.create_client = mocker.AsyncMock(return_value="client-id-1")
    panel.get_client_configuration = mocker.AsyncMock(return_value="[Interface]\n...")

    result = asyncio.run(wg.create_client_with_qr(panel, "my-phone"))

    assert result.id == "client-id-1"
    assert result.name == "my-phone"
    assert result.config_text == "[Interface]\n..."
    panel.create_client.assert_called_once_with("my-phone")
    panel.get_client_configuration.assert_called_once_with("client-id-1")
