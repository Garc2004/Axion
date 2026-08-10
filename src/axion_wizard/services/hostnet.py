"""Preparación de red del host para la variante `host` de WireGuard (§6.1).

Con `network_mode: host` en Linux nativo, el contenedor de wg-easy usa la
pila de red del propio host: los paquetes que entran por el túnel tienen que
**reenviarse** hacia nginx y hacia el resto de la LAN. Eso lo decide un
sysctl del kernel, `net.ipv4.ip_forward`, que en la mayoría de distros viene
apagado por defecto.

Con el reenvío apagado el fallo es especialmente desagradable de
diagnosticar, porque nada falla: el túnel se establece, el handshake de
WireGuard funciona, el cliente aparece conectado en el panel — y ni un solo
paquete llega a su destino. No hay error en ningún log, ni del contenedor ni
del host.

`privileges.ELEVATION_REASONS` ya anunciaba "aplicar `sysctl` de reenvío IP
para WireGuard (Linux)" y el propio `docker-compose.yml.j2` remite a
`/etc/sysctl.d/99-wireguard.conf`, pero **nadie lo escribía**: el wizard
pedía privilegios de root para un trabajo que después no hacía. Este módulo
es esa parte que faltaba.

Se escribe un archivo en `/etc/sysctl.d/` en vez de solo `sysctl -w` porque
`-w` no sobrevive a un reinicio, y el stack sí (`restart: unless-stopped`):
sin el archivo, la VPN dejaría de encaminar en el primer reboot.
"""

from __future__ import annotations

import platform as _platform
from dataclasses import dataclass
from pathlib import Path

from axion_wizard.utils.shell import CommandNotFoundError, CommandTimeoutError, run

SYSCTL_CONF_PATH = Path("/etc/sysctl.d/99-wireguard.conf")

#: `net.ipv4.ip_forward` es el que importa para el túnel IPv4, que es el que
#: monta wg-easy por defecto (`WG_DEFAULT_ADDRESS=10.8.0.x`). El de IPv6 se
#: incluye porque un cliente con IPv6 activo intentará usarlo y, sin
#: reenvío, esas conexiones se quedan colgadas en vez de caer a IPv4.
SYSCTL_SETTINGS: dict[str, str] = {
    "net.ipv4.ip_forward": "1",
    "net.ipv6.conf.all.forwarding": "1",
}

SYSCTL_CONF_HEADER = (
    "# Generado por axion-wizard para la variante `host` de WireGuard (§6.1).\n"
    "# Sin reenvío IP el túnel se establece y el handshake funciona, pero\n"
    "# ningún paquete llega a su destino — y no aparece error en ningún log.\n"
)


@dataclass
class ForwardingResult:
    """Qué se pudo hacer con el reenvío IP, para que quien llame decida.

    Nunca se lanza una excepción desde aquí: el reenvío roto deja la VPN sin
    encaminar, pero el resto del stack (Mattermost, la IA, el acceso por LAN)
    funciona perfectamente. Abortar la instalación entera por esto sería
    peor que terminarla avisando.
    """

    applied: bool
    active: bool
    conf_written: bool
    detail: str

    @property
    def needs_attention(self) -> bool:
        return not self.active


def is_applicable(os_name: str, wireguard_variant: str) -> bool:
    """El reenvío IP solo hace falta en Linux con `network_mode: host`.

    En la variante `ports` los paquetes los encamina Docker con su propio
    NAT, y en Windows/macOS el kernel que importa es el de la VM de Docker
    Desktop, no el del host — tocar sysctls ahí no tendría ningún efecto.
    """
    from axion_wizard.domain.config import WireguardVariant

    return os_name == "Linux" and wireguard_variant == WireguardVariant.HOST.value


def read_runtime_value(key: str, proc_root: Path = Path("/proc/sys")) -> str | None:
    """Valor efectivo de un sysctl leído de `/proc/sys`, o `None`.

    Se lee del kernel y no de los archivos de `/etc/sysctl.d/`: lo que
    importa es lo que está activo *ahora*, no lo que alguien dejó escrito.
    """
    path = proc_root / Path(key.replace(".", "/"))
    try:
        return path.read_text().strip()
    except OSError:
        return None


def forwarding_is_active(proc_root: Path = Path("/proc/sys")) -> bool:
    """`True` si todos los sysctls de `SYSCTL_SETTINGS` ya están aplicados.

    Un sysctl que no se puede leer no cuenta como activo: en un kernel sin
    IPv6 compilado, `net.ipv6.conf.all.forwarding` no existe.
    """
    for key, expected in SYSCTL_SETTINGS.items():
        current = read_runtime_value(key, proc_root=proc_root)
        if current is None:
            # El de IPv4 siempre existe; el de IPv6 puede no estar. Solo se
            # exige el que de verdad rompe el túnel.
            if key.startswith("net.ipv4."):
                return False
            continue
        if current != expected:
            return False
    return True


def render_sysctl_conf() -> str:
    lines = [SYSCTL_CONF_HEADER]
    lines += [f"{key} = {value}\n" for key, value in SYSCTL_SETTINGS.items()]
    return "".join(lines)


def _apply_with_sysctl(conf_path: Path, timeout: float = 15.0) -> tuple[bool, str]:
    """Aplica el archivo recién escrito sin esperar a un reinicio.

    Se intenta `sysctl -p <archivo>` primero porque es acotado y no revalida
    todo `/etc/sysctl.d`, donde otro archivo ajeno y mal formado haría
    fallar la operación entera por algo que no es cosa nuestra.
    """
    for args in (["sysctl", "-p", str(conf_path)], ["sysctl", "--system"]):
        try:
            result = run(args, timeout=timeout)
        except (CommandNotFoundError, CommandTimeoutError) as exc:
            return False, str(exc)
        if result.ok:
            return True, " ".join(args)
    return False, f"`{' '.join(args)}` terminó con error"


def ensure_ip_forwarding(
    conf_path: Path = SYSCTL_CONF_PATH,
    proc_root: Path = Path("/proc/sys"),
    dry_run: bool = False,
) -> ForwardingResult:
    """Deja el reenvío IP activo y persistente. No lanza nunca.

    Idempotente: si ya está activo y el archivo tiene el contenido correcto,
    no toca nada. Si no hay permisos —`--no-elevate`, o un `sudo` rechazado—
    se devuelve el motivo para que el paso lo enseñe como aviso.
    """
    already_active = forwarding_is_active(proc_root=proc_root)
    desired = render_sysctl_conf()

    if dry_run:
        return ForwardingResult(
            applied=False,
            active=already_active,
            conf_written=False,
            detail=f"escribiría {conf_path} y aplicaría {', '.join(SYSCTL_SETTINGS)}",
        )

    conf_written = False
    try:
        if not conf_path.exists() or conf_path.read_text(encoding="utf-8") != desired:
            conf_path.parent.mkdir(parents=True, exist_ok=True)
            conf_path.write_text(desired, encoding="utf-8")
            conf_written = True
    except OSError as exc:
        # Sin el archivo, como mucho perdemos la persistencia; puede que el
        # reenvío ya esté activo por otra vía, así que no se da por perdido.
        return ForwardingResult(
            applied=False,
            active=already_active,
            conf_written=False,
            detail=f"no se pudo escribir {conf_path} ({exc}); hace falta root",
        )

    if already_active and not conf_written:
        return ForwardingResult(
            applied=True, active=True, conf_written=False, detail="ya estaba activo"
        )

    applied, how = _apply_with_sysctl(conf_path)
    active = forwarding_is_active(proc_root=proc_root)
    if applied and active:
        return ForwardingResult(
            applied=True, active=True, conf_written=conf_written, detail=f"aplicado con {how}"
        )
    if active:
        # `sysctl` falló pero el valor está bien: da igual cómo llegó ahí.
        return ForwardingResult(
            applied=True, active=True, conf_written=conf_written, detail="activo en el kernel"
        )
    return ForwardingResult(
        applied=False,
        active=False,
        conf_written=conf_written,
        detail=f"{conf_path} escrito, pero el sysctl no llegó a aplicarse ({how})",
    )


def describe_manual_fix(conf_path: Path = SYSCTL_CONF_PATH) -> list[str]:
    """Los mismos pasos, a mano, para cuando el wizard no tiene privilegios."""
    settings = " ".join(f"{key}={value}" for key, value in SYSCTL_SETTINGS.items())
    return [
        f"Escribir {conf_path} con: {', '.join(f'{k} = {v}' for k, v in SYSCTL_SETTINGS.items())}",
        f"Aplicarlo sin reiniciar: sudo sysctl -w {settings}",
        "O relanzar el wizard con privilegios: sudo axion-wizard install",
    ]


def current_os_name() -> str:
    return _platform.system()
