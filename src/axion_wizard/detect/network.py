"""LAN IP, MAC, CGNAT and free ports (§4.2)."""

from __future__ import annotations

import platform as _platform
import socket
from contextlib import closing
from dataclasses import dataclass, field

import httpx
import psutil

from axion_wizard.utils.shell import CommandNotFoundError, CommandTimeoutError, run

REQUIRED_PORTS: list[tuple[int, str]] = [
    (51820, "udp"),
    (51821, "tcp"),
    (443, "tcp"),
    (80, "tcp"),
]

CONNECTIVITY_TARGETS: list[str] = ["registry-1.docker.io", "ollama.com"]


@dataclass
class InterfaceInfo:
    name: str
    ip: str | None
    mac: str | None


@dataclass
class PortStatus:
    port: int
    protocol: str
    in_use: bool
    used_by: str | None = None  # e.g. "docker:mm-wireguard"
    #: `False` when the system would not let us enumerate the sockets (see
    #: `PortScan.inspectable`). `in_use` is then a default with no information
    #: behind it, not an observation — anyone deciding anything from it has to
    #: tell "free" apart from "could not look".
    inspectable: bool = True

    @property
    def free(self) -> bool:
        return not self.in_use


@dataclass
class PortScan:
    """Busy ports as seen by `psutil`, plus whether we were allowed to look.

    This exists because enumerating sockets is a privileged operation on most
    systems: on Linux and macOS without root, `psutil.net_connections` raises
    `AccessDenied`. Returning empty sets in that case — which is what happened
    before — is indistinguishable from "nothing is listening", and made
    `doctor`, which does not elevate, report every port of a perfectly healthy
    stack in red.
    """

    tcp: set[int] = field(default_factory=set)
    udp: set[int] = field(default_factory=set)
    #: `False` if the OS denied enumeration: `tcp`/`udp` then mean nothing.
    inspectable: bool = True

    def contains(self, port: int, protocol: str) -> bool:
        return port in (self.tcp if protocol.lower() == "tcp" else self.udp)


def list_interfaces() -> list[InterfaceInfo]:
    infos = []
    for name, addr_list in psutil.net_if_addrs().items():
        ip = None
        mac = None
        for addr in addr_list:
            if addr.family == socket.AF_INET and ip is None:
                ip = addr.address
            elif addr.family == psutil.AF_LINK and mac is None:
                mac = addr.address
        infos.append(InterfaceInfo(name=name, ip=ip, mac=mac))
    return infos


def get_default_route_ip(timeout: float = 2.0) -> str | None:
    """IP of the interface the OS would use to reach the internet.

    A UDP `connect()` sends no packet — it only makes the kernel resolve which
    local interface it would use to reach that destination, per its routing
    table. It is the reliable signal for "primary interface", regardless of
    what the adapter happens to be called.

    This exists because on any machine with Docker Desktop, WSL2 or Hyper-V —
    this wizard's entire audience — `psutil.net_if_addrs()` almost always
    enumerates a virtual vSwitch (`vEthernet (Default Switch)`, typically
    `172.x.x.x`) before the real network card: taking "the first one with an
    IP" produced an address reachable only from the host itself, never from a
    phone or another machine on the LAN — which is precisely what needs to
    reach the WireGuard panel or Mattermost.
    """
    with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as probe:
        try:
            probe.settimeout(timeout)
            # 8.8.8.8:80 is only a reference destination for resolving the
            # route; no real connection is made and nothing is transmitted.
            probe.connect(("8.8.8.8", 80))
            return probe.getsockname()[0]
        except OSError:
            return None


def get_primary_interface(prefer_name: str | None = None) -> InterfaceInfo | None:
    interfaces = list_interfaces()
    if prefer_name:
        for iface in interfaces:
            if iface.name == prefer_name and iface.ip:
                return iface

    default_ip = get_default_route_ip()
    if default_ip:
        for iface in interfaces:
            if iface.ip == default_ip:
                return iface

    # With no resolvable default route (a sandbox with no network, say): fall
    # back to the previous behaviour as a last resort, not as the normal path.
    for iface in interfaces:
        if iface.ip and not iface.ip.startswith("127."):
            return iface
    return None


async def get_public_ipv4(timeout: float = 5.0) -> str | None:
    """Outbound public IP, forcing IPv4 (avoids false CGNAT on dual-stack hosts)."""
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
        try:
            response = await client.get("https://api.ipify.org?format=json")
            response.raise_for_status()
            return response.json().get("ip")
        except httpx.HTTPError:
            return None


def parse_network_category(output: str) -> str | None:
    """Extract the category (`Public`/`Private`/`DomainAuthenticated`) from
    the output of `Get-NetConnectionProfile`, with or without a table
    header."""
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.upper().startswith(("INTERFACEALIAS", "---", "NETWORKCATEGORY")):
            continue
        parts = line.split()
        if parts:
            return parts[-1]
    return None


def get_windows_network_category(
    interface_name: str | None = None, timeout: float = 10.0
) -> str | None:
    """Windows network category (`Public`/`Private`/`DomainAuthenticated`).

    It matters because the "Public" profile applies
    `BlockInbound,AllowOutbound` by default in the Windows Firewall — with
    WSL2 *mirrored networking* active, that alone silently blocks LAN access
    to the ports Docker Desktop publishes, even when the wizard's own firewall
    rules are correctly in place (§4.1/§6.5).
    """
    if _platform.system() != "Windows":
        return None
    command = "Get-NetConnectionProfile | Select-Object -ExpandProperty NetworkCategory"
    if interface_name:
        escaped = interface_name.replace("'", "''")
        command = (
            f"Get-NetConnectionProfile -InterfaceAlias '{escaped}' "
            "| Select-Object -ExpandProperty NetworkCategory"
        )
    try:
        result = run(["powershell", "-NoProfile", "-Command", command], timeout=timeout)
    except (CommandNotFoundError, CommandTimeoutError):
        return None
    if not result.ok:
        return None
    return parse_network_category(result.stdout)


def is_cgnat(public_ip: str | None, router_wan_ip: str | None) -> bool:
    """The router cannot be read reliably, so `router_wan_ip` is supplied by
    the user, copied from its admin panel."""
    if not public_ip or not router_wan_ip:
        return False
    return public_ip.strip() != router_wan_ip.strip()


def scan_bound_ports() -> PortScan:
    """Busy ports on this host, split by protocol.

    TCP and UDP are detected differently on purpose: a UDP socket has no
    `LISTEN` state (psutil reports it as `NONE`), so filtering on
    `status == CONN_LISTEN` — the obvious approach — would leave out **every**
    UDP socket, WireGuard's 51820 included. `conn.type` (SOCK_STREAM /
    SOCK_DGRAM) is used to tell them apart instead.

    If the OS denies enumeration (Linux/macOS without root), a `PortScan` with
    `inspectable=False` is returned rather than an empty one: they are
    different things, and confusing them produced false negatives in `doctor`.
    """
    scan = PortScan()
    try:
        connections = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, PermissionError):
        scan.inspectable = False
        return scan

    for conn in connections:
        if not conn.laddr:
            continue
        port = conn.laddr.port
        if conn.type == socket.SOCK_STREAM:
            if conn.status == psutil.CONN_LISTEN:
                scan.tcp.add(port)
        elif conn.type == socket.SOCK_DGRAM:
            # A bound UDP socket already occupies the port; no state to check.
            scan.udp.add(port)
    return scan


def bound_ports_by_protocol() -> dict[str, set[int]]:
    """`scan_bound_ports` as a plain dict, dropping whether we could look.
    Kept for callers that only want the ports; any path that needs to tell
    "free" from "could not check" must use `scan_bound_ports` directly."""
    scan = scan_bound_ports()
    return {"tcp": scan.tcp, "udp": scan.udp}


def check_ports_psutil(ports: list[tuple[int, str]] | None = None) -> list[PortStatus]:
    """Busy ports as seen from this host (listening TCP and bound UDP).

    It does not see those published by Docker Desktop containers (they live in
    another VM) — see `merge_docker_published_ports`.
    """
    ports = ports if ports is not None else REQUIRED_PORTS
    scan = scan_bound_ports()

    statuses = []
    for port, proto in ports:
        statuses.append(
            PortStatus(
                port=port,
                protocol=proto,
                in_use=scan.contains(port, proto),
                inspectable=scan.inspectable,
            )
        )
    return statuses


def parse_docker_ports_field(ports_field: str) -> list[tuple[int, str]]:
    """Parse the `Ports` field of `docker ps`, e.g.
    `0.0.0.0:443->443/tcp, 0.0.0.0:51820->51820/udp`."""
    results: list[tuple[int, str]] = []
    if not ports_field:
        return results
    for chunk in ports_field.split(","):
        chunk = chunk.strip()
        if "->" not in chunk or "/" not in chunk:
            continue
        _, right = chunk.split("->", 1)
        port_str, _, proto = right.partition("/")
        try:
            port = int(port_str.strip())
        except ValueError:
            continue
        results.append((port, proto.strip().lower()))
    return results


def merge_docker_published_ports(
    statuses: list[PortStatus], docker_ps_output: list[dict]
) -> list[PortStatus]:
    """Critical note from §4.2: under Docker Desktop, `psutil`/`ss` cannot see
    ports published by containers. Always supplement with
    `docker ps --format json`."""
    published: dict[tuple[int, str], str] = {}
    for container in docker_ps_output:
        name = container.get("Names") or container.get("Name") or "?"
        for mapping in parse_docker_ports_field(container.get("Ports", "")):
            published[mapping] = name

    merged = []
    for status in statuses:
        key = (status.port, status.protocol)
        if key in published:
            merged.append(
                PortStatus(
                    port=status.port,
                    protocol=status.protocol,
                    in_use=True,
                    used_by=f"docker:{published[key]}",
                    # Docker did tell us, so this particular port is a real
                    # observation even if `psutil` was not allowed to look.
                    inspectable=True,
                )
            )
        else:
            merged.append(status)
    return merged


async def check_connectivity(
    targets: list[str] | None = None, timeout: float = 5.0
) -> dict[str, bool]:
    """Reachability of the hosts that `docker pull` and `ollama pull` depend
    on later in the flow."""
    targets = targets if targets is not None else CONNECTIVITY_TARGETS
    results: dict[str, bool] = {}
    async with httpx.AsyncClient(timeout=timeout) as client:
        for target in targets:
            try:
                response = await client.head(f"https://{target}", follow_redirects=True)
                results[target] = response.status_code < 500
            except httpx.HTTPError:
                results[target] = False
    return results
