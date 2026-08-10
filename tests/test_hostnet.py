"""Reenvío IP del host para la variante `host` de WireGuard (§6.1).

Contexto de por qué existe este módulo y estos tests: el wizard pedía
privilegios de root citando "aplicar `sysctl` de reenvío IP para WireGuard",
y el compose remitía a `/etc/sysctl.d/99-wireguard.conf`, pero nada en el
código escribía ese archivo. Con el reenvío apagado el fallo es mudo: el
túnel se establece, el handshake funciona, el panel muestra el cliente
conectado — y no pasa un solo paquete, sin error en ningún log.
"""

from pathlib import Path

import pytest

from axion_wizard.domain.config import WireguardVariant
from axion_wizard.services import hostnet


def _proc_root(tmp_path: Path, ipv4: str = "1", ipv6: str | None = "1") -> Path:
    """Simula `/proc/sys` para no depender del kernel de quien corre los tests."""
    root = tmp_path / "proc"
    ipv4_path = root / "net" / "ipv4"
    ipv4_path.mkdir(parents=True)
    (ipv4_path / "ip_forward").write_text(f"{ipv4}\n")
    if ipv6 is not None:
        ipv6_path = root / "net" / "ipv6" / "conf" / "all"
        ipv6_path.mkdir(parents=True)
        (ipv6_path / "forwarding").write_text(f"{ipv6}\n")
    return root


# --- cuándo aplica ------------------------------------------------------------------


def test_applies_only_to_linux_host_variant() -> None:
    assert hostnet.is_applicable("Linux", WireguardVariant.HOST.value) is True


@pytest.mark.parametrize(
    ("os_name", "variant"),
    [
        ("Linux", WireguardVariant.PORTS.value),
        ("Windows", WireguardVariant.HOST.value),
        ("Windows", WireguardVariant.PORTS.value),
        ("Darwin", WireguardVariant.HOST.value),
    ],
)
def test_does_not_apply_elsewhere(os_name: str, variant: str) -> None:
    """En `ports` encamina Docker con su NAT, y en Windows/macOS el kernel
    que importa es el de la VM de Docker Desktop, no el del host."""
    assert hostnet.is_applicable(os_name, variant) is False


# --- lectura del estado real --------------------------------------------------------


def test_forwarding_is_active_when_both_sysctls_are_on(tmp_path: Path) -> None:
    assert hostnet.forwarding_is_active(proc_root=_proc_root(tmp_path)) is True


def test_forwarding_is_inactive_when_ipv4_is_off(tmp_path: Path) -> None:
    assert hostnet.forwarding_is_active(proc_root=_proc_root(tmp_path, ipv4="0")) is False


def test_a_kernel_without_ipv6_still_counts_as_active(tmp_path: Path) -> None:
    """En un kernel sin IPv6 compilado ese sysctl no existe; exigirlo daría
    un falso negativo permanente."""
    assert hostnet.forwarding_is_active(proc_root=_proc_root(tmp_path, ipv6=None)) is True


def test_missing_ipv4_sysctl_is_not_active(tmp_path: Path) -> None:
    assert hostnet.forwarding_is_active(proc_root=tmp_path / "no-existe") is False


# --- escritura y aplicación ----------------------------------------------------------


def test_writes_the_conf_and_reports_active(tmp_path: Path, mocker) -> None:
    conf = tmp_path / "99-wireguard.conf"
    mocker.patch("axion_wizard.services.hostnet._apply_with_sysctl", return_value=(True, "sysctl"))

    result = hostnet.ensure_ip_forwarding(conf_path=conf, proc_root=_proc_root(tmp_path))

    assert result.active is True
    assert result.needs_attention is False
    assert conf.exists()
    assert "net.ipv4.ip_forward = 1" in conf.read_text(encoding="utf-8")


def test_persists_across_reboots_by_writing_to_sysctl_d(tmp_path: Path, mocker) -> None:
    """`sysctl -w` a secas no sobrevive a un reinicio, y el stack sí
    (`restart: unless-stopped`): sin el archivo la VPN dejaría de encaminar
    en el primer reboot."""
    conf = tmp_path / "99-wireguard.conf"
    mocker.patch("axion_wizard.services.hostnet._apply_with_sysctl", return_value=(True, "sysctl"))

    hostnet.ensure_ip_forwarding(conf_path=conf, proc_root=_proc_root(tmp_path, ipv4="0"))

    assert conf.read_text(encoding="utf-8") == hostnet.render_sysctl_conf()


def test_is_idempotent_when_already_active(tmp_path: Path, mocker) -> None:
    conf = tmp_path / "99-wireguard.conf"
    conf.write_text(hostnet.render_sysctl_conf(), encoding="utf-8")
    apply_mock = mocker.patch("axion_wizard.services.hostnet._apply_with_sysctl")

    result = hostnet.ensure_ip_forwarding(conf_path=conf, proc_root=_proc_root(tmp_path))

    assert result.active is True
    assert result.conf_written is False
    apply_mock.assert_not_called()


def test_reports_missing_privileges_instead_of_raising(tmp_path: Path, mocker) -> None:
    """Sin root no se puede escribir en `/etc/sysctl.d`. Eso deja la VPN sin
    encaminar, pero Mattermost y la IA funcionan igual: abortar la
    instalación entera sería peor que terminarla avisando."""
    # El `/proc` simulado se construye ANTES de romper `write_text`, o lo
    # rompería también a él.
    proc_root = _proc_root(tmp_path, ipv4="0")
    mocker.patch.object(Path, "write_text", side_effect=PermissionError("denegado"))

    result = hostnet.ensure_ip_forwarding(
        conf_path=tmp_path / "99-wireguard.conf", proc_root=proc_root
    )

    assert result.active is False
    assert result.needs_attention is True
    assert "root" in result.detail


def test_reports_when_sysctl_did_not_take_effect(tmp_path: Path, mocker) -> None:
    conf = tmp_path / "99-wireguard.conf"
    mocker.patch(
        "axion_wizard.services.hostnet._apply_with_sysctl", return_value=(False, "no encontrado")
    )

    result = hostnet.ensure_ip_forwarding(
        conf_path=conf, proc_root=_proc_root(tmp_path, ipv4="0")
    )

    assert result.active is False
    assert result.conf_written is True


def test_active_kernel_wins_even_if_sysctl_command_failed(tmp_path: Path, mocker) -> None:
    """Si el valor está bien, da igual cómo llegó ahí."""
    conf = tmp_path / "99-wireguard.conf"
    mocker.patch("axion_wizard.services.hostnet._apply_with_sysctl", return_value=(False, "err"))

    result = hostnet.ensure_ip_forwarding(conf_path=conf, proc_root=_proc_root(tmp_path))

    assert result.active is True


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    conf = tmp_path / "99-wireguard.conf"
    result = hostnet.ensure_ip_forwarding(
        conf_path=conf, proc_root=_proc_root(tmp_path), dry_run=True
    )
    assert not conf.exists()
    assert result.applied is False


def test_manual_fix_mentions_the_conf_and_the_command() -> None:
    steps = hostnet.describe_manual_fix(Path("/etc/sysctl.d/99-wireguard.conf"))
    joined = " ".join(steps)
    assert "99-wireguard.conf" in joined
    assert "net.ipv4.ip_forward" in joined


# --- integración con el paso 6 -------------------------------------------------------


def _context_with_environment(tmp_path: Path, os_name: str, variant: str):
    """Contexto con hechos reales, no mocks: `Mock(name=...)` no fija el
    atributo `name` —lo usa para nombrar el propio mock— y `OsInfo.name` es
    justo lo que decide si el reenvío aplica."""
    from axion_wizard.detect.docker import DockerContextInfo, DockerInfo
    from axion_wizard.detect.hardware import HardwareInfo
    from axion_wizard.detect.platform import OsInfo, WslInfo
    from axion_wizard.steps.context import EnvironmentFacts, InstallContext

    context = InstallContext(project_dir=tmp_path)
    context.environment = EnvironmentFacts(
        os_info=OsInfo(name=os_name, release="6.1"),
        wsl=WslInfo(inside_wsl=False),
        docker=DockerInfo(
            installed=True,
            docker_version="28.0",
            compose_version="2.30",
            compose_is_v2=True,
            context=DockerContextInfo(active_context="default", is_desktop=False),
        ),
        hardware=HardwareInfo(ram_total_bytes=8 * 1024**3, cpu_logical=4, cpu_physical=4),
        wireguard_variant=variant,
    )
    return context


def test_deploy_step_skips_forwarding_on_the_ports_variant(tmp_path: Path, mocker) -> None:
    from axion_wizard.cli import GlobalState
    from axion_wizard.steps.s06_deploy import DeployStep

    ensure = mocker.patch("axion_wizard.services.hostnet.ensure_ip_forwarding")
    context = _context_with_environment(tmp_path, "Windows", WireguardVariant.PORTS.value)

    DeployStep(GlobalState(project_dir=tmp_path), context)._ensure_host_ip_forwarding()

    ensure.assert_not_called()


def test_deploy_step_applies_forwarding_on_the_host_variant(tmp_path: Path, mocker) -> None:
    from axion_wizard.cli import GlobalState
    from axion_wizard.steps.s06_deploy import DeployStep

    ensure = mocker.patch(
        "axion_wizard.services.hostnet.ensure_ip_forwarding",
        return_value=hostnet.ForwardingResult(
            applied=True, active=True, conf_written=True, detail="aplicado"
        ),
    )
    context = _context_with_environment(tmp_path, "Linux", WireguardVariant.HOST.value)

    DeployStep(GlobalState(project_dir=tmp_path), context)._ensure_host_ip_forwarding()

    ensure.assert_called_once()
    assert context.warnings == []


def test_deploy_step_warns_but_does_not_abort_when_forwarding_fails(
    tmp_path: Path, mocker
) -> None:
    from axion_wizard.cli import GlobalState
    from axion_wizard.steps.s06_deploy import DeployStep

    mocker.patch(
        "axion_wizard.services.hostnet.ensure_ip_forwarding",
        return_value=hostnet.ForwardingResult(
            applied=False, active=False, conf_written=False, detail="hace falta root"
        ),
    )
    context = _context_with_environment(tmp_path, "Linux", WireguardVariant.HOST.value)

    DeployStep(GlobalState(project_dir=tmp_path), context)._ensure_host_ip_forwarding()

    assert len(context.warnings) == 1
    assert "no packet" in context.warnings[0]
