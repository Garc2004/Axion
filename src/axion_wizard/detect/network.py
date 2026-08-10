"""IP LAN, MAC, CGNAT y puertos libres (§4.2)."""

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
    used_by: str | None = None  # p.ej. "docker:mm-wireguard"
    #: `False` cuando el sistema no dejó enumerar los sockets (ver
    #: `PortScan.inspectable`). `in_use` es entonces un valor por defecto sin
    #: información detrás, no una observación — quien decida algo a partir de
    #: él tiene que distinguir "libre" de "no se pudo mirar".
    inspectable: bool = True

    @property
    def free(self) -> bool:
        return not self.in_use


@dataclass
class PortScan:
    """Puertos ocupados vistos por `psutil`, con la salvedad de si se pudo mirar.

    Existe porque enumerar sockets es una operación privilegiada en buena
    parte de los sistemas: en Linux y macOS sin root, `psutil.net_connections`
    lanza `AccessDenied`. Devolver conjuntos vacíos en ese caso —lo que se
    hacía antes— es indistinguible de "no hay nada escuchando", y hacía que
    `doctor`, que no eleva, reportara en rojo todos los puertos de un stack
    perfectamente sano.
    """

    tcp: set[int] = field(default_factory=set)
    udp: set[int] = field(default_factory=set)
    #: `False` si el SO denegó la enumeración: `tcp`/`udp` no significan nada.
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
    """IP de la interfaz que el SO usaría para salir a internet.

    `connect()` en UDP no envía ningún paquete — solo hace que el kernel
    resuelva qué interfaz local usaría para alcanzar ese destino, según su
    tabla de rutas. Es la señal fiable de "interfaz principal", indistinta
    de cómo se llame el adaptador.

    Existe porque en cualquier máquina con Docker Desktop, WSL2 o Hyper-V
    —el público objetivo de este wizard— `psutil.net_if_addrs()` casi
    siempre enumera antes un vSwitch virtual (`vEthernet (Default Switch)`,
    típicamente `172.x.x.x`) que la tarjeta de red real: tomar "el primero
    con IP" daba una dirección solo alcanzable desde el propio host, nunca
    desde el móvil ni otro equipo de la LAN — justo lo que necesita acceder
    al panel de WireGuard o a Mattermost.
    """
    with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as probe:
        try:
            probe.settimeout(timeout)
            # 8.8.8.8:80 es solo un destino de referencia para resolver la
            # ruta; no se establece conexión real ni se transmite nada.
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

    # Sin ruta por defecto resoluble (sandbox sin red, etc.): se cae al
    # comportamiento anterior como último recurso, no como camino normal.
    for iface in interfaces:
        if iface.ip and not iface.ip.startswith("127."):
            return iface
    return None


async def get_public_ipv4(timeout: float = 5.0) -> str | None:
    """IP pública saliente, forzando IPv4 (evita falsos CGNAT en hosts dual-stack)."""
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
        try:
            response = await client.get("https://api.ipify.org?format=json")
            response.raise_for_status()
            return response.json().get("ip")
        except httpx.HTTPError:
            return None


def parse_network_category(output: str) -> str | None:
    """Extrae la categoría (`Public`/`Private`/`DomainAuthenticated`) de la
    salida de `Get-NetConnectionProfile`, con o sin cabecera de tabla."""
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
    """Categoría de red de Windows (`Public`/`Private`/`DomainAuthenticated`).

    Importa porque el perfil "Public" aplica por defecto
    `BlockInbound,AllowOutbound` en el Firewall de Windows — con *mirrored
    networking* de WSL2 activo, eso basta para bloquear en silencio el
    acceso LAN a los puertos que Docker Desktop publica, aunque las reglas
    de firewall específicas del wizard estén bien puestas (§4.1/§6.5).
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
    """No se puede leer el router de forma fiable, así que `router_wan_ip` lo
    aporta el usuario copiándolo del panel de administración."""
    if not public_ip or not router_wan_ip:
        return False
    return public_ip.strip() != router_wan_ip.strip()


def scan_bound_ports() -> PortScan:
    """Puertos ocupados en este host, separados por protocolo.

    TCP y UDP se detectan de forma distinta a propósito: un socket UDP no
    tiene estado `LISTEN` (psutil lo reporta como `NONE`), así que filtrar
    por `status == CONN_LISTEN` — como haría el camino obvio — dejaría
    fuera **todos** los sockets UDP, incluido el 51820 de WireGuard. Se
    distingue por `conn.type` (SOCK_STREAM / SOCK_DGRAM) en su lugar.

    Si el SO deniega la enumeración (Linux/macOS sin root), se devuelve un
    `PortScan` con `inspectable=False` en vez de uno vacío: son cosas
    distintas y confundirlas producía falsos negativos en `doctor`.
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
            # Un socket UDP enlazado ya ocupa el puerto; no hay estado que mirar.
            scan.udp.add(port)
    return scan


def bound_ports_by_protocol() -> dict[str, set[int]]:
    """Vista de `scan_bound_ports` como diccionario, sin el dato de si se
    pudo mirar. Se conserva para quien solo quiera los puertos; el camino
    que necesita distinguir "libre" de "no se pudo comprobar" debe usar
    `scan_bound_ports` directamente."""
    scan = scan_bound_ports()
    return {"tcp": scan.tcp, "udp": scan.udp}


def check_ports_psutil(ports: list[tuple[int, str]] | None = None) -> list[PortStatus]:
    """Puertos ocupados vistos desde este host (TCP en escucha y UDP enlazados).

    No ve los publicados por contenedores de Docker Desktop (viven en otra
    VM), ver `merge_docker_published_ports`.
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
    """Parsea el campo `Ports` de `docker ps`, p.ej.
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
    """Nota crítica de §4.2: bajo Docker Desktop, `psutil`/`ss` no ven los
    puertos publicados por contenedores. Complementar siempre con
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
                    # Docker sí nos lo contó, así que este puerto concreto es
                    # una observación real aunque `psutil` no pudiera mirar.
                    inspectable=True,
                )
            )
        else:
            merged.append(status)
    return merged


async def check_connectivity(
    targets: list[str] | None = None, timeout: float = 5.0
) -> dict[str, bool]:
    """Alcanzabilidad de los hosts de los que dependen `docker pull` y
    `ollama pull` más adelante en el flujo."""
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
