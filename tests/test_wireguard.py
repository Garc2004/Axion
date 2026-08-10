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

    with pytest.raises(NetworkError, match="did not answer"):
        asyncio.run(
            wg.wait_for_panel_ready("http://x:51821", timeout=0.05, wait_min=0.01, wait_max=0.02)
        )


# --- _client_id_from_creation -----------------------------------------------------
#
# La v14 no devolvia el cliente creado, solo `{"success": true}`, asi que el
# id habia que deducirlo listando antes y despues y comparando — con la
# ambiguedad extra de que wg-easy nunca ha exigido nombres unicos. La v15 lo
# devuelve en la respuesta y todo ese rodeo desaparecio.


def test_client_id_from_creation_reads_the_field() -> None:
    response = httpx.Response(200, json={"success": True, "clientId": "abc"})
    assert wg._client_id_from_creation(response) == "abc"


def test_client_id_from_creation_accepts_a_numeric_id() -> None:
    """El esquema lo declara como el identificador de la fila; dar por hecho
    que siempre llega serializado como texto es justo el tipo de suposicion
    que este modulo evita en el resto de respuestas."""
    response = httpx.Response(200, json={"success": True, "clientId": 42})
    assert wg._client_id_from_creation(response) == "42"


def test_client_id_from_creation_without_the_field_is_none() -> None:
    assert wg._client_id_from_creation(httpx.Response(200, json={"success": True})) is None


def test_client_id_from_creation_on_non_json_is_none() -> None:
    assert wg._client_id_from_creation(httpx.Response(200, text="<html>")) is None


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
    with pytest.raises(DeploymentError, match="not shaped as expected"):
        asyncio.run(panel.list_clients())


def test_list_clients_rejects_non_json_body(mocker) -> None:
    mock_inner = _inner_client(
        mocker, get=mocker.AsyncMock(return_value=httpx.Response(200, text="not json"))
    )
    panel = _client_with_mocked_httpx(mocker, mock_inner)
    with pytest.raises(DeploymentError, match="valid JSON"):
        asyncio.run(panel.list_clients())


def test_list_clients_http_error_raises_deployment_error(mocker) -> None:
    mock_inner = _inner_client(
        mocker, get=mocker.AsyncMock(return_value=httpx.Response(500, text="boom"))
    )
    panel = _client_with_mocked_httpx(mocker, mock_inner)
    with pytest.raises(DeploymentError, match="Could not list"):
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
    post = mocker.AsyncMock(return_value=httpx.Response(200, json={"status": "success"}))
    panel = _client_with_mocked_httpx(mocker, _inner_client(mocker, post=post))
    asyncio.run(panel.login("admin", "correct-password"))  # no debe lanzar


def test_login_sends_username_and_remember(mocker) -> None:
    """`remember` esta declarado `z.boolean()` sin `.optional()`: omitirlo
    devuelve un 400 de validacion que no menciona el campo que falta."""
    post = mocker.AsyncMock(return_value=httpx.Response(200, json={"status": "success"}))
    panel = _client_with_mocked_httpx(mocker, _inner_client(mocker, post=post))
    asyncio.run(panel.login("admin", "correct-password"))

    path, kwargs = post.call_args[0][0], post.call_args[1]
    assert path == "/api/session"
    assert kwargs["json"] == {
        "username": "admin",
        "password": "correct-password",
        "remember": False,
    }


def test_login_wrong_password_raises_deployment_error(mocker) -> None:
    mock_inner = _inner_client(mocker, post=mocker.AsyncMock(return_value=httpx.Response(401)))
    panel = _client_with_mocked_httpx(mocker, mock_inner)
    with pytest.raises(DeploymentError, match="rejected"):
        asyncio.run(panel.login("admin", "wrong-password"))


def test_login_totp_required_is_not_treated_as_success(mocker) -> None:
    """Un 200 no significa haber entrado: con 2FA el panel responde 200 y
    `TOTP_REQUIRED`, sin sesion. Darlo por bueno dejaba al wizard llamando a
    /api/client sin autenticar y fallando despues con un 401 confuso."""
    post = mocker.AsyncMock(
        return_value=httpx.Response(200, json={"status": "TOTP_REQUIRED"})
    )
    panel = _client_with_mocked_httpx(mocker, _inner_client(mocker, post=post))
    with pytest.raises(DeploymentError, match="second factor"):
        asyncio.run(panel.login("admin", "correct-password"))


def test_login_server_error_raises_deployment_error(mocker) -> None:
    mock_inner = _inner_client(
        mocker, post=mocker.AsyncMock(return_value=httpx.Response(500, text="boom"))
    )
    panel = _client_with_mocked_httpx(mocker, mock_inner)
    with pytest.raises(DeploymentError):
        asyncio.run(panel.login("admin", "x"))


def test_login_connection_error_raises_network_error(mocker) -> None:
    mock_inner = _inner_client(
        mocker, post=mocker.AsyncMock(side_effect=httpx.ConnectError("refused"))
    )
    panel = _client_with_mocked_httpx(mocker, mock_inner)
    with pytest.raises(NetworkError):
        asyncio.run(panel.login("admin", "x"))


# --- WireguardPanelClient.create_client / get_client_configuration ---------------


def test_create_client_success(mocker) -> None:
    """La v15 devuelve `clientId` en la respuesta al POST: ni se lista antes
    ni se lista despues."""
    post = mocker.AsyncMock(
        return_value=httpx.Response(200, json={"success": True, "clientId": "abc"})
    )
    get = mocker.AsyncMock()
    panel = _client_with_mocked_httpx(mocker, _inner_client(mocker, get=get, post=post))

    assert asyncio.run(panel.create_client("my-phone")) == "abc"
    assert post.call_args[0][0] == "/api/client"
    assert post.call_args[1]["json"] == {"name": "my-phone"}
    get.assert_not_awaited()


def test_create_client_without_a_client_id_raises_deployment_error(mocker) -> None:
    """Defensivo: si el POST responde exito pero sin `clientId`, el contrato
    cambio — mejor fallar accionable que devolver un id equivocado."""
    post = mocker.AsyncMock(return_value=httpx.Response(200, json={"success": True}))
    panel = _client_with_mocked_httpx(mocker, _inner_client(mocker, post=post))
    with pytest.raises(DeploymentError, match="did not return the id"):
        asyncio.run(panel.create_client("my-phone"))


def test_create_client_http_error_raises_deployment_error(mocker) -> None:
    mock_inner = _inner_client(
        mocker, post=mocker.AsyncMock(return_value=httpx.Response(400, text="bad name"))
    )
    panel = _client_with_mocked_httpx(mocker, mock_inner)
    with pytest.raises(DeploymentError, match="could not create"):
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
    with pytest.raises(NetworkError, match="Could not reach"):
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
