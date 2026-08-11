import asyncio
import socket
from contextlib import closing

import httpx
import pytest

from axion_wizard.detect import network as net


def test_is_cgnat_true_when_ips_differ() -> None:
    assert net.is_cgnat("203.0.113.45", "100.64.12.8") is True


def test_is_cgnat_false_when_ips_match() -> None:
    assert net.is_cgnat("203.0.113.45", "203.0.113.45") is False


def test_is_cgnat_false_when_missing_data() -> None:
    assert net.is_cgnat(None, "203.0.113.45") is False
    assert net.is_cgnat("203.0.113.45", None) is False


def test_parse_docker_ports_field_multiple() -> None:
    field = "0.0.0.0:443->443/tcp, 0.0.0.0:51820->51820/udp"
    assert net.parse_docker_ports_field(field) == [(443, "tcp"), (51820, "udp")]


def test_parse_docker_ports_field_empty() -> None:
    assert net.parse_docker_ports_field("") == []


def test_parse_docker_ports_field_ignores_non_published() -> None:
    assert net.parse_docker_ports_field("6379/tcp") == []


def test_parse_docker_ports_field_ignores_unparseable_port() -> None:
    assert net.parse_docker_ports_field("0.0.0.0:abc->abc/tcp") == []


def test_get_primary_interface_prefers_named_interface(mocker) -> None:
    """`prefer_name` beats even the default route: it is an explicit choice by
    the caller, stronger than any heuristic."""
    fake_interfaces = [
        net.InterfaceInfo(name="lo", ip="127.0.0.1", mac=None),
        net.InterfaceInfo(name="eth0", ip="192.168.1.50", mac="aa:bb:cc:dd:ee:ff"),
    ]
    mocker.patch("axion_wizard.detect.network.list_interfaces", return_value=fake_interfaces)
    iface = net.get_primary_interface(prefer_name="eth0")
    assert iface is not None
    assert iface.name == "eth0"


def test_get_primary_interface_picks_the_default_route_interface_over_the_first_listed(
    mocker,
) -> None:
    """A real regression: on a machine with Docker Desktop/WSL2, psutil lists
    the Hyper-V vSwitch (`vEthernet (Default Switch)`, 172.21.48.1) BEFORE the
    real network card (`Ethernet`, 192.168.1.65). Choosing "the first one with
    an IP" gave an address reachable only from the host itself — never from a
    phone or another machine on the LAN, which is precisely who needs to reach
    the WireGuard panel or Mattermost."""
    fake_interfaces = [
        net.InterfaceInfo(name="vEthernet (Default Switch)", ip="172.21.48.1", mac=None),
        net.InterfaceInfo(name="Ethernet", ip="192.168.1.65", mac="aa:bb:cc:dd:ee:ff"),
        net.InterfaceInfo(name="lo", ip="127.0.0.1", mac=None),
    ]
    mocker.patch("axion_wizard.detect.network.list_interfaces", return_value=fake_interfaces)
    mocker.patch(
        "axion_wizard.detect.network.get_default_route_ip", return_value="192.168.1.65"
    )

    iface = net.get_primary_interface()

    assert iface is not None
    assert iface.name == "Ethernet"
    assert iface.ip == "192.168.1.65"


def test_get_primary_interface_falls_back_when_default_route_is_unresolvable(mocker) -> None:
    """With no network (an isolated sandbox, a VM with no route out),
    `get_default_route_ip` returns `None`: it falls back to the previous
    behaviour as a last resort."""
    fake_interfaces = [
        net.InterfaceInfo(name="lo", ip="127.0.0.1", mac=None),
        net.InterfaceInfo(name="wlan0", ip="192.168.1.50", mac="aa:bb:cc:dd:ee:ff"),
    ]
    mocker.patch("axion_wizard.detect.network.list_interfaces", return_value=fake_interfaces)
    mocker.patch("axion_wizard.detect.network.get_default_route_ip", return_value=None)

    iface = net.get_primary_interface()

    assert iface is not None
    assert iface.name == "wlan0"


def test_get_primary_interface_falls_back_when_default_route_matches_nothing(mocker) -> None:
    """The default-route IP need not match any of `list_interfaces` exactly
    (adapters enumerated differently by each API); in that case it still falls
    back to the first non-loopback one."""
    fake_interfaces = [
        net.InterfaceInfo(name="lo", ip="127.0.0.1", mac=None),
        net.InterfaceInfo(name="wlan0", ip="192.168.1.50", mac="aa:bb:cc:dd:ee:ff"),
    ]
    mocker.patch("axion_wizard.detect.network.list_interfaces", return_value=fake_interfaces)
    mocker.patch(
        "axion_wizard.detect.network.get_default_route_ip", return_value="10.0.0.9"
    )

    iface = net.get_primary_interface()

    assert iface is not None
    assert iface.name == "wlan0"


def test_get_primary_interface_none_when_only_loopback(mocker) -> None:
    fake_interfaces = [net.InterfaceInfo(name="lo", ip="127.0.0.1", mac=None)]
    mocker.patch("axion_wizard.detect.network.list_interfaces", return_value=fake_interfaces)
    mocker.patch("axion_wizard.detect.network.get_default_route_ip", return_value=None)
    assert net.get_primary_interface() is None


# --- get_default_route_ip ------------------------------------------------------------


def test_get_default_route_ip_reads_the_connected_sockets_local_address(mocker) -> None:
    """It must send no real traffic: the socket is replaced by a double that
    only exposes `getsockname()`, to confirm the value is read from there and
    from no other source."""
    fake_socket = mocker.Mock()
    fake_socket.getsockname.return_value = ("192.168.1.65", 54321)
    fake_socket.__enter__ = mocker.Mock(return_value=fake_socket)
    fake_socket.__exit__ = mocker.Mock(return_value=False)
    mocker.patch("axion_wizard.detect.network.socket.socket", return_value=fake_socket)

    assert net.get_default_route_ip() == "192.168.1.65"
    fake_socket.connect.assert_called_once_with(("8.8.8.8", 80))


def test_get_default_route_ip_returns_none_without_a_route(mocker) -> None:
    fake_socket = mocker.Mock()
    fake_socket.connect.side_effect = OSError("Network is unreachable")
    fake_socket.__enter__ = mocker.Mock(return_value=fake_socket)
    fake_socket.__exit__ = mocker.Mock(return_value=False)
    mocker.patch("axion_wizard.detect.network.socket.socket", return_value=fake_socket)

    assert net.get_default_route_ip() is None


def test_get_default_route_ip_never_sends_real_traffic() -> None:
    """Against the real network stack: it must return something (or None)
    instantly, without blocking and without depending on 8.8.8.8 answering —
    a UDP `connect()` only resolves the local route, it never transmits a
    packet."""
    result = net.get_default_route_ip(timeout=1.0)
    assert result is None or isinstance(result, str)


def test_check_ports_psutil_handles_access_denied(mocker) -> None:
    import psutil

    mocker.patch(
        "axion_wizard.detect.network.psutil.net_connections",
        side_effect=psutil.AccessDenied(),
    )
    statuses = net.check_ports_psutil([(443, "tcp")])
    assert statuses[0].in_use is False


def _free_port(kind: int) -> int:
    """Bind and release a port to obtain one we know is free."""
    with closing(socket.socket(socket.AF_INET, kind)) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_check_ports_psutil_detects_a_really_bound_udp_port() -> None:
    """Regression: a UDP socket has no LISTEN state (psutil reports it as
    NONE), so filtering on CONN_LISTEN left WireGuard's 51820/udp invisible —
    §4.2's conflict check never detected it and `doctor` always reported it
    missing in the `host` variant."""
    port = _free_port(socket.SOCK_DGRAM)
    with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as sock:
        sock.bind(("127.0.0.1", port))
        statuses = net.check_ports_psutil([(port, "udp")])

    assert statuses[0].in_use is True, "a genuinely bound UDP port must show as in use"


def test_check_ports_psutil_detects_a_really_bound_tcp_port() -> None:
    port = _free_port(socket.SOCK_STREAM)
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
        statuses = net.check_ports_psutil([(port, "tcp")])

    assert statuses[0].in_use is True


def test_check_ports_psutil_reports_unbound_port_as_free(mocker) -> None:
    """With no bound sockets, every port queried comes back free.

    The connection list is mocked on purpose. The previous version asked the
    kernel for an ephemeral port, released it, and assumed it would still be
    free when checked; on Linux, with the ephemeral range heavily used, that
    same number could already be taken by another process and the test failed
    because of the environment, not the code.
    """
    mocker.patch("axion_wizard.detect.network.psutil.net_connections", return_value=[])
    statuses = net.check_ports_psutil([(51820, "udp"), (443, "tcp")])
    assert [s.free for s in statuses] == [True, True]


def test_check_ports_psutil_does_not_confuse_protocols() -> None:
    """A port in use over UDP must not be reported as in use over TCP."""
    port = _free_port(socket.SOCK_DGRAM)
    with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as sock:
        sock.bind(("127.0.0.1", port))
        statuses = {s.protocol: s for s in net.check_ports_psutil([(port, "udp"), (port, "tcp")])}

    assert statuses["udp"].in_use is True
    assert statuses["tcp"].in_use is False


@pytest.mark.parametrize("proto", ["TCP", "UDP"])
def test_check_ports_psutil_protocol_matching_is_case_insensitive(proto: str) -> None:
    kind = socket.SOCK_STREAM if proto.lower() == "tcp" else socket.SOCK_DGRAM
    port = _free_port(kind)
    with closing(socket.socket(socket.AF_INET, kind)) as sock:
        sock.bind(("127.0.0.1", port))
        if kind == socket.SOCK_STREAM:
            sock.listen(1)
        statuses = net.check_ports_psutil([(port, proto)])

    assert statuses[0].in_use is True


def test_bound_ports_by_protocol_returns_both_keys_on_access_denied(mocker) -> None:
    import psutil

    mocker.patch(
        "axion_wizard.detect.network.psutil.net_connections",
        side_effect=psutil.AccessDenied(),
    )
    bound = net.bound_ports_by_protocol()
    assert bound == {"tcp": set(), "udp": set()}


def test_merge_docker_published_ports_marks_container_owned() -> None:
    statuses = [
        net.PortStatus(port=443, protocol="tcp", in_use=False),
        net.PortStatus(port=80, protocol="tcp", in_use=False),
    ]
    docker_ps = [{"Names": "mm-nginx", "Ports": "0.0.0.0:443->443/tcp"}]
    merged = net.merge_docker_published_ports(statuses, docker_ps)
    by_port = {s.port: s for s in merged}
    assert by_port[443].in_use is True
    assert by_port[443].used_by == "docker:mm-nginx"
    assert by_port[80].in_use is False


def test_port_status_free_property() -> None:
    assert net.PortStatus(port=80, protocol="tcp", in_use=False).free is True
    assert net.PortStatus(port=80, protocol="tcp", in_use=True).free is False


def test_list_interfaces_returns_at_least_loopback() -> None:
    interfaces = net.list_interfaces()
    assert len(interfaces) >= 1


def test_check_ports_psutil_returns_status_for_every_requested_port() -> None:
    ports = [(51820, "udp"), (443, "tcp")]
    statuses = net.check_ports_psutil(ports)
    assert [(s.port, s.protocol) for s in statuses] == ports


def test_get_public_ipv4_success(mocker) -> None:
    mock_response = mocker.Mock()
    mock_response.raise_for_status = mocker.Mock()
    mock_response.json.return_value = {"ip": "203.0.113.10"}

    mock_client = mocker.AsyncMock()
    mock_client.get = mocker.AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = mocker.AsyncMock(return_value=False)
    mocker.patch("axion_wizard.detect.network.httpx.AsyncClient", return_value=mock_client)

    ip = asyncio.run(net.get_public_ipv4())
    assert ip == "203.0.113.10"


def test_get_public_ipv4_failure_returns_none(mocker) -> None:
    mock_client = mocker.AsyncMock()
    mock_client.get = mocker.AsyncMock(side_effect=httpx.ConnectError("boom"))
    mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = mocker.AsyncMock(return_value=False)
    mocker.patch("axion_wizard.detect.network.httpx.AsyncClient", return_value=mock_client)

    ip = asyncio.run(net.get_public_ipv4())
    assert ip is None


def test_check_connectivity_mixed_results(mocker) -> None:
    ok_response = mocker.Mock(status_code=200)

    async def fake_head(url, follow_redirects=True):
        if "docker.io" in url:
            return ok_response
        raise httpx.ConnectError("unreachable")

    mock_client = mocker.AsyncMock()
    mock_client.head = fake_head
    mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = mocker.AsyncMock(return_value=False)
    mocker.patch("axion_wizard.detect.network.httpx.AsyncClient", return_value=mock_client)

    results = asyncio.run(net.check_connectivity(["registry-1.docker.io", "ollama.com"]))
    assert results == {"registry-1.docker.io": True, "ollama.com": False}


# --- get_windows_network_category / parse_network_category ---------------------------


def test_parse_network_category_single_value() -> None:
    """`Get-NetConnectionProfile | Select-Object -ExpandProperty NetworkCategory`
    prints the value alone, with no table header."""
    assert net.parse_network_category("Private\n") == "Private"


def test_parse_network_category_skips_table_header() -> None:
    """The alternative format (without -ExpandProperty), just in case."""
    output = "NetworkCategory\n---------------\nPublic\n"
    assert net.parse_network_category(output) == "Public"


def test_parse_network_category_empty_output() -> None:
    assert net.parse_network_category("") is None
    assert net.parse_network_category("   \n") is None


def test_get_windows_network_category_none_outside_windows(mocker) -> None:
    mocker.patch("axion_wizard.detect.network._platform.system", return_value="Linux")
    assert net.get_windows_network_category() is None


def test_get_windows_network_category_parses_powershell_output(mocker) -> None:
    from axion_wizard.utils.shell import CommandResult

    mocker.patch("axion_wizard.detect.network._platform.system", return_value="Windows")
    run_mock = mocker.patch(
        "axion_wizard.detect.network.run",
        return_value=CommandResult(args=[], returncode=0, stdout="Public\n", stderr=""),
    )
    assert net.get_windows_network_category("Ethernet") == "Public"
    command = run_mock.call_args[0][0]
    assert "-InterfaceAlias 'Ethernet'" in command[-1]


def test_get_windows_network_category_none_when_powershell_fails(mocker) -> None:
    from axion_wizard.utils.shell import CommandNotFoundError

    mocker.patch("axion_wizard.detect.network._platform.system", return_value="Windows")
    mocker.patch(
        "axion_wizard.detect.network.run", side_effect=CommandNotFoundError("powershell")
    )
    assert net.get_windows_network_category() is None


def test_get_windows_network_category_none_when_command_errors(mocker) -> None:
    from axion_wizard.utils.shell import CommandResult

    mocker.patch("axion_wizard.detect.network._platform.system", return_value="Windows")
    mocker.patch(
        "axion_wizard.detect.network.run",
        return_value=CommandResult(args=[], returncode=1, stdout="", stderr="boom"),
    )
    assert net.get_windows_network_category() is None


# --- enumerating sockets is a privileged operation --------------------------------
#
# Regression: `AccessDenied` (Linux/macOS without root) was swallowed and
# empty sets returned, indistinguishable from "nothing is listening". `doctor`
# does not elevate, so a healthy stack was reported with every port in red.


def test_scan_reports_when_it_could_not_inspect(mocker) -> None:
    import psutil

    mocker.patch("psutil.net_connections", side_effect=psutil.AccessDenied())
    scan = net.scan_bound_ports()
    assert scan.inspectable is False
    assert scan.tcp == set()


def test_scan_is_inspectable_when_enumeration_works(mocker) -> None:
    mocker.patch("psutil.net_connections", return_value=[])
    assert net.scan_bound_ports().inspectable is True


def test_check_ports_propagates_the_inspectable_flag(mocker) -> None:
    import psutil

    mocker.patch("psutil.net_connections", side_effect=psutil.AccessDenied())
    statuses = net.check_ports_psutil([(443, "tcp")])
    assert statuses[0].inspectable is False
    # `in_use=False` here is not an observation, it is the absence of one.
    assert statuses[0].in_use is False


def test_docker_published_ports_are_inspectable_even_without_privileges(mocker) -> None:
    """Docker did tell us, so that particular port really is a finding."""
    import psutil

    mocker.patch("psutil.net_connections", side_effect=psutil.AccessDenied())
    statuses = net.check_ports_psutil([(443, "tcp")])
    merged = net.merge_docker_published_ports(
        statuses, [{"Names": "axion-nginx-1", "Ports": "0.0.0.0:443->443/tcp"}]
    )
    assert merged[0].in_use is True
    assert merged[0].inspectable is True
