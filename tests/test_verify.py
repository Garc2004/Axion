import asyncio
from pathlib import Path

import httpx

from axion_wizard.detect.network import PortStatus
from axion_wizard.domain.config import WireguardVariant
from axion_wizard.errors import ConfigError
from axion_wizard.services.compose import ContainerStatus
from axion_wizard.steps import s09_verify as verify
from axion_wizard.utils.shell import CommandResult

# --- check_containers_healthy ------------------------------------------------------


def test_check_containers_healthy_all_ok(mocker) -> None:
    mocker.patch(
        "axion_wizard.steps.s09_verify.compose.ps",
        return_value=[
            ContainerStatus(service="postgres", name="p", state="running", health="healthy")
        ],
    )
    result = verify.check_containers_healthy(Path("x"))
    assert result.ok is True


def test_check_containers_healthy_reports_unhealthy(mocker) -> None:
    mocker.patch(
        "axion_wizard.steps.s09_verify.compose.ps",
        return_value=[
            ContainerStatus(service="postgres", name="p", state="running", health="healthy"),
            ContainerStatus(service="mattermost", name="m", state="running", health="unhealthy"),
        ],
    )
    result = verify.check_containers_healthy(Path("x"))
    assert result.ok is False
    assert "mattermost" in result.detail


def test_check_containers_healthy_empty_ps_output(mocker) -> None:
    mocker.patch("axion_wizard.steps.s09_verify.compose.ps", return_value=[])
    result = verify.check_containers_healthy(Path("x"))
    assert result.ok is False


# --- check_https_responds ----------------------------------------------------------


def test_check_https_responds_ok(mocker) -> None:
    mock_response = mocker.Mock(status_code=200)
    mock_client = mocker.AsyncMock()
    mock_client.get = mocker.AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = mocker.AsyncMock(return_value=False)
    mocker.patch("axion_wizard.steps.s09_verify.httpx.AsyncClient", return_value=mock_client)

    result = asyncio.run(verify.check_https_responds("192.168.1.50"))
    assert result.ok is True


def test_check_https_responds_connection_error(mocker) -> None:
    mock_client = mocker.AsyncMock()
    mock_client.get = mocker.AsyncMock(side_effect=httpx.ConnectError("refused"))
    mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = mocker.AsyncMock(return_value=False)
    mocker.patch("axion_wizard.steps.s09_verify.httpx.AsyncClient", return_value=mock_client)

    # retry_timeout corto: sin esto, el reintento por defecto (20s, pensado
    # para la latencia real de LAN bajo Docker Desktop/WSL2) haría que un
    # fallo que sabemos permanente tardara 20s en confirmarse.
    result = asyncio.run(verify.check_https_responds("192.168.1.50", retry_timeout=0.1))
    assert result.ok is False


# --- check_cert_has_san -------------------------------------------------------------


def test_check_cert_has_san_missing_file(tmp_path: Path) -> None:
    result = verify.check_cert_has_san(tmp_path / "nope.crt")
    assert result.ok is False
    assert "no existe" in result.detail


def test_check_cert_has_san_ok(mocker, tmp_path: Path) -> None:
    cert_path = tmp_path / "cert.crt"
    cert_path.write_text("fake")
    mocker.patch(
        "axion_wizard.steps.s09_verify.certs.verify_certificate_has_san",
        return_value=["IP:192.168.1.50", "DNS:axion.local"],
    )
    result = verify.check_cert_has_san(cert_path)
    assert result.ok is True
    assert "axion.local" in result.detail


def test_check_cert_has_san_invalid_cert(mocker, tmp_path: Path) -> None:
    cert_path = tmp_path / "cert.crt"
    cert_path.write_text("fake")
    mocker.patch(
        "axion_wizard.steps.s09_verify.certs.verify_certificate_has_san",
        side_effect=ConfigError(what="sin SAN", why="y", steps=[]),
    )
    result = verify.check_cert_has_san(cert_path)
    assert result.ok is False
    assert result.detail == "sin SAN"


# --- check_webhook_reachable ---------------------------------------------------------


def test_check_webhook_reachable_ok(mocker) -> None:
    mocker.patch(
        "axion_wizard.steps.s09_verify.compose.exec_in_service",
        return_value=CommandResult(args=[], returncode=0, stdout='{"status":"ok"}', stderr=""),
    )
    result = verify.check_webhook_reachable(Path("x"))
    assert result.ok is True


def test_check_webhook_reachable_failure(mocker) -> None:
    mocker.patch(
        "axion_wizard.steps.s09_verify.compose.exec_in_service",
        return_value=CommandResult(args=[], returncode=1, stdout="", stderr="connection refused"),
    )
    result = verify.check_webhook_reachable(Path("x"))
    assert result.ok is False
    assert "refused" in result.detail


def test_check_webhook_reachable_uses_curl_not_wget(mocker) -> None:
    """Regresión real: la imagen de mattermost-team-edition no trae wget
    (mismo hallazgo que el healthcheck del propio servicio) — con wget esta
    comprobación fallaba siempre con 'executable file not found in $PATH',
    indistinguible de un webhook genuinamente inalcanzable."""
    exec_mock = mocker.patch(
        "axion_wizard.steps.s09_verify.compose.exec_in_service",
        return_value=CommandResult(args=[], returncode=0, stdout='{"status":"ok"}', stderr=""),
    )
    verify.check_webhook_reachable(Path("x"))
    command = exec_mock.call_args[0][2]
    assert "wget" not in command
    assert command[0] == "curl"


# --- check_model_loaded ---------------------------------------------------------------


def test_check_model_loaded_present(mocker) -> None:
    mocker.patch(
        "axion_wizard.steps.s09_verify.ollama_service.list_installed_models",
        mocker.AsyncMock(return_value=[{"name": "qwen2.5:1.5b"}]),
    )
    result = asyncio.run(verify.check_model_loaded("qwen2.5:1.5b"))
    assert result.ok is True


def test_check_model_loaded_missing(mocker) -> None:
    mocker.patch(
        "axion_wizard.steps.s09_verify.ollama_service.list_installed_models",
        mocker.AsyncMock(return_value=[{"name": "other-model"}]),
    )
    result = asyncio.run(verify.check_model_loaded("qwen2.5:1.5b"))
    assert result.ok is False


# --- check_wireguard_panel ----------------------------------------------------------


def test_check_wireguard_panel_ok(mocker) -> None:
    mock_response = mocker.Mock(status_code=200)
    mock_client = mocker.AsyncMock()
    mock_client.get = mocker.AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = mocker.AsyncMock(return_value=False)
    mocker.patch("axion_wizard.steps.s09_verify.httpx.AsyncClient", return_value=mock_client)

    result = asyncio.run(verify.check_wireguard_panel("192.168.1.50"))
    assert result.ok is True


def test_check_wireguard_panel_unreachable(mocker) -> None:
    mock_client = mocker.AsyncMock()
    mock_client.get = mocker.AsyncMock(side_effect=httpx.ConnectError("refused"))
    mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = mocker.AsyncMock(return_value=False)
    mocker.patch("axion_wizard.steps.s09_verify.httpx.AsyncClient", return_value=mock_client)

    result = asyncio.run(verify.check_wireguard_panel("192.168.1.50", retry_timeout=0.1))
    assert result.ok is False


def test_check_wireguard_panel_retries_transient_failures(mocker) -> None:
    """Regresión real: bajo Docker Desktop/WSL2, el panel podía tardar
    varios segundos más de lo que cubre un único intento aunque funcionara
    perfectamente — `doctor` lo marcaba FALLO mientras el navegador (o
    `install`, que sí reintenta) lo veía sin problema."""
    mock_ok = mocker.Mock(status_code=200)
    mock_client = mocker.AsyncMock()
    mock_client.get = mocker.AsyncMock(side_effect=[httpx.ConnectError("refused"), mock_ok])
    mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = mocker.AsyncMock(return_value=False)
    mocker.patch("axion_wizard.steps.s09_verify.httpx.AsyncClient", return_value=mock_client)

    result = asyncio.run(
        verify.check_wireguard_panel("192.168.1.50", retry_timeout=5.0)
    )

    assert result.ok is True
    assert mock_client.get.call_count == 2


# --- check_published_ports -----------------------------------------------------------


def test_check_published_ports_variant_ports_ok(mocker) -> None:
    mocker.patch(
        "axion_wizard.steps.s09_verify.compose.ps",
        return_value=[
            ContainerStatus(
                service="nginx", name="n", state="running", health=None, published_ports=[80, 443]
            ),
            ContainerStatus(
                service="wireguard",
                name="w",
                state="running",
                health=None,
                published_ports=[51820, 51821],
            ),
        ],
    )
    result = verify.check_published_ports(Path("x"), WireguardVariant.PORTS.value)
    assert result.ok is True


def test_check_published_ports_variant_ports_missing_wireguard(mocker) -> None:
    mocker.patch(
        "axion_wizard.steps.s09_verify.compose.ps",
        return_value=[
            ContainerStatus(
                service="nginx", name="n", state="running", health=None, published_ports=[80, 443]
            ),
        ],
    )
    result = verify.check_published_ports(Path("x"), WireguardVariant.PORTS.value)
    assert result.ok is False
    assert "wireguard:51820" in result.detail


def test_check_published_ports_variant_host_uses_psutil(mocker) -> None:
    mocker.patch(
        "axion_wizard.steps.s09_verify.compose.ps",
        return_value=[
            ContainerStatus(
                service="nginx", name="n", state="running", health=None, published_ports=[80, 443]
            ),
        ],
    )
    from axion_wizard.detect.network import PortStatus

    mocker.patch(
        "axion_wizard.steps.s09_verify.detect_network.check_ports_psutil",
        return_value=[
            PortStatus(port=51820, protocol="udp", in_use=True),
            PortStatus(port=51821, protocol="tcp", in_use=True),
        ],
    )
    result = verify.check_published_ports(Path("x"), WireguardVariant.HOST.value)
    assert result.ok is True


def test_check_published_ports_variant_host_psutil_reports_missing(mocker) -> None:
    mocker.patch(
        "axion_wizard.steps.s09_verify.compose.ps",
        return_value=[
            ContainerStatus(
                service="nginx", name="n", state="running", health=None, published_ports=[80, 443]
            ),
        ],
    )
    from axion_wizard.detect.network import PortStatus

    mocker.patch(
        "axion_wizard.steps.s09_verify.detect_network.check_ports_psutil",
        return_value=[
            PortStatus(port=51820, protocol="udp", in_use=False),
            PortStatus(port=51821, protocol="tcp", in_use=True),
        ],
    )
    result = verify.check_published_ports(Path("x"), WireguardVariant.HOST.value)
    assert result.ok is False
    assert "wireguard:51820" in result.detail


# --- run_all_checks / all_checks_passed / render_checks_table -----------------------


def test_all_checks_passed_true_when_all_ok() -> None:
    results = [verify.CheckResult("a", True), verify.CheckResult("b", True)]
    assert verify.all_checks_passed(results) is True


def test_all_checks_passed_false_when_one_fails() -> None:
    results = [verify.CheckResult("a", True), verify.CheckResult("b", False)]
    assert verify.all_checks_passed(results) is False


def test_render_checks_table_contains_all_check_names() -> None:
    results = [
        verify.CheckResult("Contenedores healthy", True, "ok"),
        verify.CheckResult("HTTPS responde", False, "boom"),
    ]
    table = verify.render_checks_table(results)
    assert table.row_count == 2


def test_run_all_checks_calls_every_check(mocker) -> None:
    facts = verify.DeploymentFacts(
        project_dir=Path("x"),
        compose_path=Path("x/docker-compose.yml"),
        cert_path=Path("x/nginx/certs/cert.crt"),
        host="192.168.1.50",
        ollama_model="qwen2.5:1.5b",
        wireguard_variant=WireguardVariant.PORTS.value,
    )

    mocker.patch(
        "axion_wizard.steps.s09_verify.check_containers_healthy",
        return_value=verify.CheckResult("Contenedores healthy", True),
    )
    mocker.patch(
        "axion_wizard.steps.s09_verify.check_https_responds",
        mocker.AsyncMock(return_value=verify.CheckResult("HTTPS responde", True)),
    )
    mocker.patch(
        "axion_wizard.steps.s09_verify.check_cert_has_san",
        return_value=verify.CheckResult("Cert tiene SAN", True),
    )
    mocker.patch(
        "axion_wizard.steps.s09_verify.check_webhook_reachable",
        return_value=verify.CheckResult("Webhook alcanzable", True),
    )
    mocker.patch(
        "axion_wizard.steps.s09_verify.check_model_loaded",
        mocker.AsyncMock(return_value=verify.CheckResult("Modelo cargado", True)),
    )
    mocker.patch(
        "axion_wizard.steps.s09_verify.check_wireguard_panel",
        mocker.AsyncMock(return_value=verify.CheckResult("Panel WireGuard", True)),
    )
    mocker.patch(
        "axion_wizard.steps.s09_verify.check_published_ports",
        return_value=verify.CheckResult("Puertos publicados", True),
    )
    mocker.patch(
        "axion_wizard.steps.s09_verify.check_websocket",
        return_value=verify.CheckResult("WebSocket Mattermost", True),
    )

    results = asyncio.run(verify.run_all_checks(facts))
    assert len(results) == 9
    assert verify.all_checks_passed(results) is True
    names = [r.name for r in results]
    assert "WebSocket Mattermost" in names
    assert "Reenvío IP (WireGuard)" in names


# --- puertos: sin privilegios no es lo mismo que "faltan" --------------------------


def test_published_ports_does_not_fail_when_it_cannot_inspect(mocker) -> None:
    """`doctor` no eleva: en Linux sin root `psutil` deniega la enumeración y
    todos los puertos de la variante `host` se reportaban como ausentes."""
    mocker.patch(
        "axion_wizard.steps.s09_verify.compose.ps",
        return_value=[
            ContainerStatus(
                service="nginx", name="n", state="running", health=None,
                published_ports=[80, 443],
            )
        ],
    )
    mocker.patch(
        "axion_wizard.steps.s09_verify.detect_network.check_ports_psutil",
        return_value=[
            PortStatus(port=51820, protocol="udp", in_use=False, inspectable=False),
            PortStatus(port=51821, protocol="tcp", in_use=False, inspectable=False),
        ],
    )

    result = verify.check_published_ports(Path("x"), WireguardVariant.HOST.value)

    assert result.ok is True
    assert "privilegios" in result.detail


def test_published_ports_still_fails_when_a_port_is_really_missing(mocker) -> None:
    mocker.patch(
        "axion_wizard.steps.s09_verify.compose.ps",
        return_value=[
            ContainerStatus(
                service="nginx", name="n", state="running", health=None,
                published_ports=[80, 443],
            )
        ],
    )
    mocker.patch(
        "axion_wizard.steps.s09_verify.detect_network.check_ports_psutil",
        return_value=[
            PortStatus(port=51820, protocol="udp", in_use=False, inspectable=True),
            PortStatus(port=51821, protocol="tcp", in_use=True, inspectable=True),
        ],
    )

    result = verify.check_published_ports(Path("x"), WireguardVariant.HOST.value)

    assert result.ok is False
    assert "51820" in result.detail


# --- WebSocket: el check que distingue "no responde" de "solo responde con F5" -----
#
# Mattermost empuja los mensajes nuevos por WebSocket. Si ese canal está roto
# pero HTTP funciona, la respuesta de la IA se escribe en el canal y el
# navegador no se entera hasta que se recarga la página — el síntoma exacto
# que llevó a añadir esta comprobación. `doctor` solo miraba `GET https://`,
# que es justo el tráfico que sí funciona en ese escenario.


def test_websocket_ok_on_a_101_handshake(mocker) -> None:
    mocker.patch(
        "axion_wizard.steps.s09_verify._websocket_handshake_status",
        return_value=(101, "HTTP/1.1 101 Switching Protocols"),
    )
    result = verify.check_websocket("192.168.1.50")
    assert result.ok is True
    assert "101" in result.detail


def test_websocket_failure_points_at_siteurl_when_rejected(mocker) -> None:
    mocker.patch(
        "axion_wizard.steps.s09_verify._websocket_handshake_status",
        return_value=(403, "HTTP/1.1 403 Forbidden"),
    )
    result = verify.check_websocket("192.168.1.50")
    assert result.ok is False
    assert "MM_SITEURL" in result.detail


def test_websocket_failure_names_the_mirrored_stall_on_a_timeout(mocker) -> None:
    """Un handshake que no llega a responder es la firma de moby/moby#48201,
    y el arreglo (volver a NAT) es el opuesto al de un rechazo por config."""
    mocker.patch(
        "axion_wizard.steps.s09_verify._websocket_handshake_status",
        return_value=(None, "TimeoutError: timed out"),
    )
    result = verify.check_websocket("192.168.1.50")
    assert result.ok is False
    assert "48201" in result.detail
    assert "F5" in result.detail
