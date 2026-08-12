"""Tests for the individual steps of the install flow."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from axion_wizard.cli import GlobalState
from axion_wizard.detect.docker import DockerContextInfo, DockerInfo
from axion_wizard.detect.hardware import HardwareInfo
from axion_wizard.detect.platform import OsInfo, WslInfo
from axion_wizard.domain.config import AccessMode, AxionConfig, WireguardVariant
from axion_wizard.errors import DeploymentError, PlatformError
from axion_wizard.steps.context import EnvironmentFacts, InstallContext


def _docker_info(*, installed=True, compose_v2=True, desktop=False) -> DockerInfo:
    return DockerInfo(
        installed=installed,
        docker_version="Docker version 27.0.0" if installed else None,
        compose_version="2.29.0" if compose_v2 else "1.29.2",
        compose_is_v2=compose_v2,
        context=DockerContextInfo(
            active_context="desktop-linux" if desktop else "default",
            is_desktop=desktop,
            contexts=[],
        ),
    )


def _environment(tmp_path: Path, variant=WireguardVariant.PORTS) -> EnvironmentFacts:
    return EnvironmentFacts(
        os_info=OsInfo(name="Windows", release="11"),
        wsl=WslInfo(inside_wsl=False),
        docker=_docker_info(),
        hardware=HardwareInfo(ram_total_bytes=16 * 1024**3, cpu_logical=8, cpu_physical=4),
        wireguard_variant=variant.value,
    )


def _config(tmp_path: Path, variant=WireguardVariant.PORTS) -> AxionConfig:
    return AxionConfig(
        access_mode=AccessMode.LAN,
        host="192.168.1.50",
        wireguard_variant=variant,
        postgres_password="a" * 64,
        wireguard_admin_username="admin",
        wireguard_admin_password="correct-horse-battery-staple",
        ollama_model="qwen2.5:1.5b",
        project_dir=tmp_path,
    )


def _context(tmp_path: Path, variant=WireguardVariant.PORTS) -> InstallContext:
    context = InstallContext(project_dir=tmp_path)
    context.environment = _environment(tmp_path, variant)
    context.config = _config(tmp_path, variant)
    return context


# --- paso 1: entorno ----------------------------------------------------------------


def test_environment_step_aborts_without_docker(tmp_path: Path, mocker) -> None:
    from axion_wizard.steps.s01_environment import EnvironmentStep

    mocker.patch(
        "axion_wizard.steps.s01_environment.detect_docker.gather_docker_info",
        return_value=_docker_info(installed=False),
    )
    mocker.patch("axion_wizard.steps.s01_environment.detect_hardware")
    step = EnvironmentStep(GlobalState(project_dir=tmp_path), InstallContext(tmp_path))

    with pytest.raises(PlatformError, match="Docker"):
        step.run()


def test_environment_step_aborts_on_compose_v1(tmp_path: Path, mocker) -> None:
    """Compose v1 fails further on with unreadable schema errors."""
    from axion_wizard.steps.s01_environment import EnvironmentStep

    mocker.patch(
        "axion_wizard.steps.s01_environment.detect_docker.gather_docker_info",
        return_value=_docker_info(compose_v2=False),
    )
    mocker.patch("axion_wizard.steps.s01_environment.detect_hardware")
    step = EnvironmentStep(GlobalState(project_dir=tmp_path), InstallContext(tmp_path))

    with pytest.raises(PlatformError, match="Compose v2"):
        step.run()


def test_environment_step_picks_the_ports_variant_under_docker_desktop(
    tmp_path: Path, mocker
) -> None:
    """§4.1's decisive output: everything else in the flow depends on it."""
    from axion_wizard.steps.s01_environment import EnvironmentStep

    mocker.patch(
        "axion_wizard.steps.s01_environment.detect_docker.gather_docker_info",
        return_value=_docker_info(desktop=True),
    )
    mocker.patch(
        "axion_wizard.steps.s01_environment.detect_platform.get_os_info",
        return_value=OsInfo(name="Linux", release="6.6"),
    )
    mocker.patch(
        "axion_wizard.steps.s01_environment.detect_platform.gather_wsl_info",
        return_value=WslInfo(inside_wsl=False),
    )
    mocker.patch(
        "axion_wizard.steps.s01_environment.detect_hardware",
        return_value=HardwareInfo(ram_total_bytes=8 * 1024**3, cpu_logical=4, cpu_physical=4),
    )
    # The `ports` variant makes `run()` probe IPv6 netfilter support for real;
    # mocked here so the test does not shell out to an actual `docker run`.
    mocker.patch(
        "axion_wizard.steps.s01_environment.detect_docker.docker_ipv6_netfilter_works",
        return_value=True,
    )
    context = InstallContext(tmp_path)
    step = EnvironmentStep(GlobalState(project_dir=tmp_path, quiet=True), context)

    result = step.run()

    assert result.data["wireguard_variant"] == "ports"
    assert context.require_environment().wireguard_variant == "ports"


def _env_step(tmp_path: Path, mocker, **docker_kwargs):
    """`EnvironmentStep` with the network/hardware/Docker calls mocked out,
    ready for the warning methods to be invoked directly."""
    from axion_wizard.steps.s01_environment import EnvironmentStep

    mocker.patch(
        "axion_wizard.steps.s01_environment.detect_docker.gather_docker_info",
        return_value=_docker_info(**docker_kwargs),
    )
    context = InstallContext(tmp_path)
    return EnvironmentStep(GlobalState(project_dir=tmp_path, quiet=True), context)


# --- LAN exposure warning under Docker Desktop on Windows ----------------------------
#
# Regression from a real incident: a deployment with Docker publishing the ports
# and the firewall configured correctly still would not answer from the LAN,
# because axion-wizard.exe runs natively on Windows (not inside WSL), and the
# existing warning (`_warn_about_broken_mirrored`) requires `wsl.inside_wsl` —
# it was never evaluated in the commonest case.


def test_lan_exposure_warning_skipped_when_inside_wsl(tmp_path: Path, mocker) -> None:
    """`_warn_about_broken_mirrored` already covers that case; no warning twice."""
    from axion_wizard.detect.platform import OsInfo, WslInfo

    step = _env_step(tmp_path, mocker, desktop=True)
    step._warn_about_windows_docker_desktop_lan_exposure(
        OsInfo(name="Windows", release="11"),
        WslInfo(inside_wsl=True),
        _docker_info(desktop=True),
        "ports",
    )
    assert step.context.warnings == []


def test_lan_exposure_warning_skipped_outside_windows(tmp_path: Path, mocker) -> None:
    from axion_wizard.detect.platform import OsInfo, WslInfo

    step = _env_step(tmp_path, mocker, desktop=True)
    step._warn_about_windows_docker_desktop_lan_exposure(
        OsInfo(name="Linux", release="6.6"),
        WslInfo(inside_wsl=False),
        _docker_info(desktop=True),
        "host",
    )
    assert step.context.warnings == []


def test_lan_exposure_warning_skipped_without_docker_desktop(tmp_path: Path, mocker) -> None:
    """Native Docker Engine (not Desktop) does not have this problem — it does
    not publish through an intermediate WSL2 VM."""
    from axion_wizard.detect.platform import OsInfo, WslInfo

    step = _env_step(tmp_path, mocker, desktop=False)
    step._warn_about_windows_docker_desktop_lan_exposure(
        OsInfo(name="Windows", release="11"),
        WslInfo(inside_wsl=False),
        _docker_info(desktop=False),
        "ports",
    )
    assert step.context.warnings == []


def test_lan_exposure_warns_when_mirrored_not_configured(tmp_path: Path, mocker) -> None:
    from axion_wizard.detect.platform import OsInfo, WslInfo

    mocker.patch(
        "axion_wizard.steps.s01_environment.detect_platform.locate_wslconfig_native",
        return_value=None,
    )
    step = _env_step(tmp_path, mocker, desktop=True)

    step._warn_about_windows_docker_desktop_lan_exposure(
        OsInfo(name="Windows", release="11"),
        WslInfo(inside_wsl=False),
        _docker_info(desktop=True),
        "ports",
    )

    assert len(step.context.warnings) == 1
    assert "mirrored" in step.context.warnings[0]


def test_lan_exposure_warns_when_network_category_is_public(tmp_path: Path, mocker) -> None:
    from axion_wizard.detect.platform import OsInfo, WslInfo

    wslconfig = tmp_path / ".wslconfig"
    wslconfig.write_text("[wsl2]\nnetworkingMode=mirrored\n")
    mocker.patch(
        "axion_wizard.steps.s01_environment.detect_platform.locate_wslconfig_native",
        return_value=wslconfig,
    )
    mocker.patch(
        "axion_wizard.detect.network.get_primary_interface",
        return_value=None,
    )
    mocker.patch(
        "axion_wizard.detect.network.get_windows_network_category",
        return_value="Public",
    )
    step = _env_step(tmp_path, mocker, desktop=True)

    step._warn_about_windows_docker_desktop_lan_exposure(
        OsInfo(name="Windows", release="11"),
        WslInfo(inside_wsl=False),
        _docker_info(desktop=True),
        "ports",
    )

    assert len(step.context.warnings) == 1
    assert "Public" in step.context.warnings[0]


def test_lan_exposure_notes_router_isolation_when_windows_config_looks_correct(
    tmp_path: Path, mocker
) -> None:
    """Mirrored on and a non-Public network: the Windows configuration looks
    right. The one thing left outside the wizard's reach is client isolation on
    the router — it is warned about anyway, without being able to confirm it
    from here."""
    from axion_wizard.detect.platform import OsInfo, WslInfo

    wslconfig = tmp_path / ".wslconfig"
    wslconfig.write_text("[wsl2]\nnetworkingMode=mirrored\n")
    mocker.patch(
        "axion_wizard.steps.s01_environment.detect_platform.locate_wslconfig_native",
        return_value=wslconfig,
    )
    mocker.patch(
        "axion_wizard.detect.network.get_primary_interface",
        return_value=None,
    )
    mocker.patch(
        "axion_wizard.detect.network.get_windows_network_category",
        return_value="Private",
    )
    step = _env_step(tmp_path, mocker, desktop=True)

    step._warn_about_windows_docker_desktop_lan_exposure(
        OsInfo(name="Windows", release="11"),
        WslInfo(inside_wsl=False),
        _docker_info(desktop=True),
        "ports",
    )

    # Two warnings: the router's client isolation and, now, mirrored
    # networking's own TCP-stall bug (moby/moby#48201) — the one that explains
    # messages only showing up on an F5 reload.
    assert len(step.context.warnings) == 2
    assert "router" in step.context.warnings[0]
    assert "isolation" in step.context.warnings[0]
    assert "F5" in step.context.warnings[1]
    assert "48201" in step.context.warnings[1]


# --- real GPU passthrough, not just its presence ------------------------------------
#
# Regression from a real incident: nvidia-smi detects a GTX 650 (Kepler, 2012)
# without trouble, but Docker cannot pass it into a container under WSL2. The
# compose file reserved the GPU for `ollama` all the same, which then sat in
# `created` forever and dragged `fastapi` down with it.


def _hardware_with(*gpus):
    from axion_wizard.detect.hardware import HardwareInfo

    return HardwareInfo(
        ram_total_bytes=16 * 1024**3, cpu_logical=8, cpu_physical=4, gpus=list(gpus)
    )


def test_gpu_passthrough_skipped_without_a_gpu(tmp_path: Path, mocker) -> None:
    """With no GPU there is nothing to probe — and probing anyway would cost an
    unnecessary image pull on the vast majority of installs."""
    nvidia = mocker.patch(
        "axion_wizard.steps.s01_environment.detect_docker.docker_gpu_passthrough_works"
    )
    rocm = mocker.patch(
        "axion_wizard.steps.s01_environment.detect_docker.docker_rocm_passthrough_works"
    )
    step = _env_step(tmp_path, mocker)

    assert step._check_gpu_passthrough(_hardware_with()) == "none"
    nvidia.assert_not_called()
    rocm.assert_not_called()


def test_gpu_passthrough_warns_when_gpu_present_but_unusable(tmp_path: Path, mocker) -> None:
    from axion_wizard.detect.hardware import GpuInfo

    mocker.patch(
        "axion_wizard.steps.s01_environment.detect_docker.docker_gpu_passthrough_works",
        return_value=False,
    )
    step = _env_step(tmp_path, mocker)

    result = step._check_gpu_passthrough(
        _hardware_with(GpuInfo(vendor="nvidia", name="GeForce GTX 650"))
    )

    assert result == "none"
    assert len(step.context.warnings) == 1
    assert "GTX 650" in step.context.warnings[0]
    assert "CPU" in step.context.warnings[0]


def test_gpu_passthrough_no_warning_when_it_works(tmp_path: Path, mocker) -> None:
    from axion_wizard.detect.hardware import GpuInfo

    mocker.patch(
        "axion_wizard.steps.s01_environment.detect_docker.docker_gpu_passthrough_works",
        return_value=True,
    )
    step = _env_step(tmp_path, mocker)

    result = step._check_gpu_passthrough(_hardware_with(GpuInfo(vendor="nvidia", name="RTX 4090")))

    assert result == "nvidia"
    assert step.context.warnings == []


def test_amd_gpu_is_probed_with_devices_not_with_the_nvidia_runtime(
    tmp_path: Path, mocker
) -> None:
    """`--gpus` belongs to the NVIDIA runtime and always comes back negative on
    an AMD machine: probing that way left the GPU unused with nothing to explain
    it."""
    from axion_wizard.detect.hardware import GpuInfo

    nvidia = mocker.patch(
        "axion_wizard.steps.s01_environment.detect_docker.docker_gpu_passthrough_works",
        return_value=False,
    )
    mocker.patch(
        "axion_wizard.steps.s01_environment.detect_docker.docker_rocm_passthrough_works",
        return_value=True,
    )
    step = _env_step(tmp_path, mocker)

    hardware = _hardware_with(GpuInfo(vendor="amd", name="Radeon RX 7900"))
    result = step._check_gpu_passthrough(hardware)

    assert result == "rocm"
    assert step.context.warnings == []
    nvidia.assert_not_called()


def test_amd_gpu_without_kernel_devices_explains_what_to_check(tmp_path: Path, mocker) -> None:
    from axion_wizard.detect.hardware import GpuInfo

    mocker.patch(
        "axion_wizard.steps.s01_environment.detect_docker.docker_rocm_passthrough_works",
        return_value=False,
    )
    step = _env_step(tmp_path, mocker)

    hardware = _hardware_with(GpuInfo(vendor="amd", name="Radeon RX 580"))
    result = step._check_gpu_passthrough(hardware)

    assert result == "none"
    assert "render" in step.context.warnings[0], "it must say which groups are needed"


def test_intel_gpu_says_there_is_no_ollama_image_instead_of_blaming_the_driver(
    tmp_path: Path, mocker
) -> None:
    """Telling someone with an Intel GPU to update their NVIDIA driver sends
    them hunting a problem that does not exist: there is no Ollama image for
    Intel, full stop."""
    from axion_wizard.detect.hardware import GpuInfo

    step = _env_step(tmp_path, mocker)

    result = step._check_gpu_passthrough(_hardware_with(GpuInfo(vendor="intel", name="Arc A770")))

    assert result == "none"
    warning = step.context.warnings[0]
    assert "Intel" in warning
    assert "NVIDIA" not in warning


# --- real IPv6 netfilter support, not assumed from the platform ---------------------
#
# Regression from a real incident: wg-easy's `PostUp` runs `ip6tables -t nat`
# unconditionally, and Docker Desktop's WSL2 kernel commonly has no `ip6_tables`
# compiled in at all. `wg-quick` runs `PostUp` as one chain, so that command's
# failure aborted the whole thing and rolled the WireGuard interface back — the
# container stayed up and the panel answered, but `wg show` came back empty and
# the image's own healthcheck failed forever, stalling step 6 indefinitely.


def test_ipv6_netfilter_skipped_for_the_host_variant(tmp_path: Path, mocker) -> None:
    """`network_mode: host` has no IPv6 handling to switch off — probing would
    only cost an extra image pull for nothing."""
    probe = mocker.patch(
        "axion_wizard.steps.s01_environment.detect_docker.docker_ipv6_netfilter_works"
    )
    step = _env_step(tmp_path, mocker)

    assert step._check_ipv6_netfilter("host") is True
    probe.assert_not_called()


def test_ipv6_netfilter_probed_for_the_ports_variant(tmp_path: Path, mocker) -> None:
    probe = mocker.patch(
        "axion_wizard.steps.s01_environment.detect_docker.docker_ipv6_netfilter_works",
        return_value=True,
    )
    step = _env_step(tmp_path, mocker)

    assert step._check_ipv6_netfilter("ports") is True
    probe.assert_called_once()


def test_ipv6_netfilter_unsupported_is_reported_but_not_a_warning(
    tmp_path: Path, mocker
) -> None:
    """Losing IPv6 inside the VPN tunnel is not a broken deployment — nobody
    relies on it for Mattermost/AI access — so it is worth mentioning but not
    worth `context.warnings`, which feeds the closing summary's warning list."""
    mocker.patch(
        "axion_wizard.steps.s01_environment.detect_docker.docker_ipv6_netfilter_works",
        return_value=False,
    )
    step = _env_step(tmp_path, mocker)

    assert step._check_ipv6_netfilter("ports") is False
    assert step.context.warnings == []


def test_model_prompt_defaults_to_the_model_already_installed(tmp_path: Path, mocker) -> None:
    """Anyone who ran `model set qwen2.5:3b` and reinstalls must not lose their
    choice by pressing Enter: the prompt came preselected on the catalogue's
    recommendation rather than on what is actually installed."""
    import questionary

    from axion_wizard.steps.s03_config import ConfigStep

    (tmp_path / ".env").write_text("OLLAMA_MODEL=qwen2.5:3b\n", encoding="utf-8")
    step = ConfigStep(GlobalState(project_dir=tmp_path, quiet=True), _context(tmp_path))
    choices = [
        questionary.Choice(title="qwen2.5:0.5b", value="qwen2.5:0.5b"),
        questionary.Choice(title="qwen2.5:3b", value="qwen2.5:3b"),
    ]

    assert step._current_model_choice(choices).value == "qwen2.5:3b"


def test_model_prompt_has_no_preference_on_a_fresh_project(tmp_path: Path) -> None:
    import questionary

    from axion_wizard.steps.s03_config import ConfigStep

    step = ConfigStep(GlobalState(project_dir=tmp_path, quiet=True), _context(tmp_path))
    choices = [questionary.Choice(title="qwen2.5:0.5b", value="qwen2.5:0.5b")]

    assert step._current_model_choice(choices) is None


def test_compose_step_only_reserves_the_gpu_when_passthrough_works(tmp_path: Path, mocker) -> None:
    """The compose file must not request the GPU just because `nvidia-smi` can
    see it — it has to be confirmed that Docker can genuinely use it, or `ollama`
    sits in `created` forever (§7, a real incident)."""
    from axion_wizard.steps.s05_compose import ComposeStep

    mocker.patch("axion_wizard.steps.s05_compose.config_validate")
    context = _context(tmp_path)
    context.environment.gpu_acceleration = "none"
    ComposeStep(GlobalState(project_dir=tmp_path, quiet=True), context).run()

    compose_text = (tmp_path / "docker-compose.yml").read_text(encoding="utf-8")
    assert "driver: nvidia" not in compose_text
    assert "/dev/kfd" not in compose_text


def test_compose_step_reserves_the_gpu_when_passthrough_is_confirmed(
    tmp_path: Path, mocker
) -> None:
    from axion_wizard.steps.s05_compose import ComposeStep

    mocker.patch("axion_wizard.steps.s05_compose.config_validate")
    context = _context(tmp_path)
    context.environment.gpu_acceleration = "nvidia"
    ComposeStep(GlobalState(project_dir=tmp_path, quiet=True), context).run()

    compose_text = (tmp_path / "docker-compose.yml").read_text(encoding="utf-8")
    assert "driver: nvidia" in compose_text


def test_compose_step_uses_the_rocm_image_and_devices_for_amd(tmp_path: Path, mocker) -> None:
    """The default image does not ship AMD's libraries: handing it `/dev/kfd`
    without changing it leaves the model on CPU all the same."""
    from axion_wizard.domain import images
    from axion_wizard.steps.s05_compose import ComposeStep

    mocker.patch("axion_wizard.steps.s05_compose.config_validate")
    context = _context(tmp_path)
    context.environment.gpu_acceleration = "rocm"
    ComposeStep(GlobalState(project_dir=tmp_path, quiet=True), context).run()

    compose_text = (tmp_path / "docker-compose.yml").read_text(encoding="utf-8")
    assert images.OLLAMA_ROCM_IMAGE in compose_text
    assert "/dev/kfd" in compose_text
    assert "/dev/dri" in compose_text
    assert "driver: nvidia" not in compose_text


def test_environment_step_warns_about_a_project_on_the_windows_mount(
    tmp_path: Path, mocker
) -> None:
    """§6.2: on /mnt/c the I/O is slow and .env's POSIX permissions are lost."""
    from axion_wizard.steps.s01_environment import EnvironmentStep

    mocker.patch(
        "axion_wizard.steps.s01_environment.detect_docker.gather_docker_info",
        return_value=_docker_info(),
    )
    mocker.patch(
        "axion_wizard.steps.s01_environment.detect_platform.get_os_info",
        return_value=OsInfo(name="Linux", release="6.6"),
    )
    mocker.patch(
        "axion_wizard.steps.s01_environment.detect_platform.gather_wsl_info",
        return_value=WslInfo(inside_wsl=True, distro_name="Ubuntu", version=2),
    )
    mocker.patch(
        "axion_wizard.steps.s01_environment.detect_hardware",
        return_value=HardwareInfo(ram_total_bytes=8 * 1024**3, cpu_logical=4, cpu_physical=4),
    )
    context = InstallContext(Path("/mnt/c/Users/alguien/axion"))
    EnvironmentStep(GlobalState(project_dir=context.project_dir, quiet=True), context).run()

    assert any("Windows filesystem" in w for w in context.warnings)


# --- step 4: certificate --------------------------------------------------------------


def test_certificate_step_adds_the_vpn_ip_to_the_san_in_host_variant(tmp_path: Path) -> None:
    """§6.1: with `network_mode: host`, 10.8.0.1 is a real host IP and the VPN
    clients come in through it — the cert has to cover it."""
    from axion_wizard.services import certs
    from axion_wizard.steps.s04_certificate import CertificateStep

    context = _context(tmp_path, WireguardVariant.HOST)
    step = CertificateStep(GlobalState(project_dir=tmp_path, quiet=True), context)

    step.run()

    san = certs.verify_certificate_has_san(step.cert_path)
    assert "IP:192.168.1.50" in san
    assert "IP:10.8.0.1" in san


def test_certificate_step_omits_the_vpn_ip_in_ports_variant(tmp_path: Path) -> None:
    """On Windows/Docker Desktop that IP only exists inside the VPN."""
    from axion_wizard.services import certs
    from axion_wizard.steps.s04_certificate import CertificateStep

    context = _context(tmp_path, WireguardVariant.PORTS)
    step = CertificateStep(GlobalState(project_dir=tmp_path, quiet=True), context)

    step.run()

    san = certs.verify_certificate_has_san(step.cert_path)
    assert "IP:192.168.1.50" in san
    assert "IP:10.8.0.1" not in san


def test_certificate_step_dry_run_writes_nothing(tmp_path: Path) -> None:
    from axion_wizard.steps.s04_certificate import CertificateStep

    context = _context(tmp_path)
    step = CertificateStep(GlobalState(project_dir=tmp_path, dry_run=True), context)

    step.run()

    assert not step.cert_path.exists()


# --- step 5: compose and files ------------------------------------------------------------


def test_compose_step_writes_the_fastapi_build_context(tmp_path: Path, mocker) -> None:
    """The compose file declares `build.context: ./fastapi`; without these
    files, `up --build` fails with an error that never names the real cause."""
    from axion_wizard.steps.s05_compose import ComposeStep

    mocker.patch("axion_wizard.steps.s05_compose.config_validate")
    context = _context(tmp_path)
    ComposeStep(GlobalState(project_dir=tmp_path, quiet=True), context).run()

    for filename in ("Dockerfile", "main.py", "requirements.txt"):
        assert (tmp_path / "fastapi" / filename).exists()


def test_fastapi_requirements_include_python_multipart(tmp_path: Path, mocker) -> None:
    """A real regression: `main.py` calls `request.form()` to read Mattermost's
    outgoing webhook, and Starlette requires `python-multipart` for that —
    without it EVERY webhook call failed with a 500 (`AssertionError: The
    python-multipart library must be installed`), deterministically on any
    install, not just on the machine where it was found."""
    from axion_wizard.steps.s05_compose import ComposeStep

    mocker.patch("axion_wizard.steps.s05_compose.config_validate")
    context = _context(tmp_path)
    ComposeStep(GlobalState(project_dir=tmp_path, quiet=True), context).run()

    requirements = (tmp_path / "fastapi" / "requirements.txt").read_text(encoding="utf-8")
    assert "python-multipart" in requirements


def test_compose_step_writes_every_artifact(tmp_path: Path, mocker) -> None:
    from axion_wizard.steps.s05_compose import ComposeStep

    mocker.patch("axion_wizard.steps.s05_compose.config_validate")
    context = _context(tmp_path)
    step = ComposeStep(GlobalState(project_dir=tmp_path, quiet=True), context)

    step.run()

    assert step.verify().ok is True
    compose_text = (tmp_path / "docker-compose.yml").read_text(encoding="utf-8")
    # §4.5: the SSRF variable always goes in, without the user knowing it exists.
    assert "fastapi:8000 fastapi" in compose_text


def test_compose_step_dry_run_writes_nothing(tmp_path: Path, mocker) -> None:
    from axion_wizard.steps.s05_compose import ComposeStep

    validate = mocker.patch("axion_wizard.steps.s05_compose.config_validate")
    context = _context(tmp_path)
    ComposeStep(GlobalState(project_dir=tmp_path, dry_run=True), context).run()

    assert not (tmp_path / "docker-compose.yml").exists()
    assert not (tmp_path / ".env").exists()
    validate.assert_not_called()


# --- step 8: wireguard -------------------------------------------------------------------


def test_wireguard_step_survives_a_panel_that_will_not_answer(
    tmp_path: Path, mocker
) -> None:
    """The stack is already up: not creating the initial client is not a
    deployment failure, and it can be done later with `wireguard add-client`.

    This branch used to be reached only when the user left the prompt empty. Now
    that the step asks nothing, it also covers the panel not answering or
    rejecting the credentials — which is where it genuinely matters not to tear
    down an install that otherwise finished fine.
    """
    from axion_wizard.errors import NetworkError
    from axion_wizard.steps.s08_wireguard import WireguardStep

    mocker.patch(
        "axion_wizard.steps.s08_wireguard.wg.wait_for_panel_ready",
        side_effect=NetworkError(what="the panel did not answer", why="timeout", steps=[]),
    )
    context = _context(tmp_path)
    step = WireguardStep(GlobalState(project_dir=tmp_path, quiet=True), context)

    result = step.run()

    assert result.ok is True
    assert any("add-client" in w for w in context.warnings)


def test_wireguard_step_uses_the_credentials_it_already_has(
    tmp_path: Path, mocker
) -> None:
    """It does not ask for the password again halfway through the install.

    Under wg-easy v14 there was no alternative — only the bcrypt hash was
    stored — so the user typed it twice in the same run for no visible reason.
    v15 wants it in plaintext, which means it is already in `AxionConfig`.
    """
    from axion_wizard.steps.s08_wireguard import WireguardStep

    mocker.patch("axion_wizard.steps.s08_wireguard.wg.wait_for_panel_ready")
    login = mocker.AsyncMock()
    panel = mocker.MagicMock()
    panel.__aenter__ = mocker.AsyncMock(return_value=panel)
    panel.__aexit__ = mocker.AsyncMock(return_value=False)
    panel.login = login
    mocker.patch(
        "axion_wizard.steps.s08_wireguard.wg.WireguardPanelClient", return_value=panel
    )
    mocker.patch(
        "axion_wizard.steps.s08_wireguard.wg.create_client_with_qr",
        new=mocker.AsyncMock(
            return_value=SimpleNamespace(id="1", name="primer-cliente", config_text="[i]")
        ),
    )
    mocker.patch("axion_wizard.steps.s08_wireguard.wg.render_qr_terminal", return_value="")
    ask = mocker.patch("questionary.password")

    context = _context(tmp_path)
    step = WireguardStep(GlobalState(project_dir=tmp_path, quiet=True), context)
    result = step.run()

    assert result.ok is True
    ask.assert_not_called()
    login.assert_awaited_once_with("admin", "correct-horse-battery-staple")


# --- step 9: Mattermost bot and webhook ----------------------------------------------
#
# There is no way to create the bot or the webhook without Mattermost's web
# interface (it exposes no API without a session, and a session requires an
# account a human already created) — so this step does not try: it stops and
# asks for the tokens. What is tested here is that the writing and the closing
# message are correct, not the creation itself, which does not exist.


def _mock_apply_targets(mocker):
    update_env = mocker.patch("axion_wizard.steps.s05_compose.update_env_value")
    deploy = mocker.patch("axion_wizard.steps.s06_deploy.deploy")
    wait_healthy = mocker.patch("axion_wizard.steps.s06_deploy.wait_for_healthy")
    return update_env, deploy, wait_healthy


def test_bot_setup_step_writes_both_tokens_and_recreates_fastapi_once(
    tmp_path: Path, mocker
) -> None:
    from axion_wizard.steps.s08b_bot_setup import BotSetupStep

    update_env, deploy, wait_healthy = _mock_apply_targets(mocker)
    mocker.patch(
        "axion_wizard.steps.s08b_bot_setup.interactive_input_available", return_value=True
    )
    ask = mocker.patch("questionary.text")
    ask.return_value.ask.side_effect = ["bot-token-123", "webhook-token-456"]
    mocker.patch("questionary.confirm").return_value.ask.return_value = True

    context = _context(tmp_path)
    step = BotSetupStep(GlobalState(project_dir=tmp_path, quiet=True), context)

    result = step.run()

    assert result.ok is True
    update_env.assert_any_call(tmp_path / ".env", "MM_BOT_TOKEN", "bot-token-123")
    update_env.assert_any_call(tmp_path / ".env", "MM_WEBHOOK_TOKEN", "webhook-token-456")
    # A single recreate for all three values, not one per value.
    deploy.assert_called_once()
    wait_healthy.assert_called_once()


def test_bot_setup_step_accepts_only_the_bot_token(tmp_path: Path, mocker) -> None:
    """The two are independent: leaving one blank must not stop the other from
    being applied."""
    from axion_wizard.steps.s08b_bot_setup import BotSetupStep

    update_env, deploy, _wait = _mock_apply_targets(mocker)
    mocker.patch(
        "axion_wizard.steps.s08b_bot_setup.interactive_input_available", return_value=True
    )
    ask = mocker.patch("questionary.text")
    ask.return_value.ask.side_effect = ["bot-token-123", ""]
    mocker.patch("questionary.confirm").return_value.ask.return_value = True

    context = _context(tmp_path)
    step = BotSetupStep(GlobalState(project_dir=tmp_path, quiet=True), context)

    result = step.run()

    assert result.ok is True
    update_env.assert_any_call(tmp_path / ".env", "MM_BOT_TOKEN", "bot-token-123")
    deploy.assert_called_once()


def test_bot_setup_step_skips_cleanly_when_both_answers_are_blank(
    tmp_path: Path, mocker
) -> None:
    """Leaving both blank is not a deployment failure: they get applied later
    with set-bot-token/set-webhook-token, as always."""
    from axion_wizard.steps.s08b_bot_setup import BotSetupStep

    update_env, deploy, _wait = _mock_apply_targets(mocker)
    mocker.patch(
        "axion_wizard.steps.s08b_bot_setup.interactive_input_available", return_value=True
    )
    ask = mocker.patch("questionary.text")
    ask.return_value.ask.side_effect = ["", ""]

    context = _context(tmp_path)
    step = BotSetupStep(GlobalState(project_dir=tmp_path, quiet=True), context)

    result = step.run()

    assert result.ok is True
    assert any("set-bot-token" in w for w in context.warnings)
    update_env.assert_not_called()
    deploy.assert_not_called()


def test_bot_setup_step_does_not_prompt_without_a_terminal(tmp_path: Path, mocker) -> None:
    from axion_wizard.steps.s08b_bot_setup import BotSetupStep

    update_env, _deploy, _wait = _mock_apply_targets(mocker)
    mocker.patch(
        "axion_wizard.steps.s08b_bot_setup.interactive_input_available", return_value=False
    )
    ask = mocker.patch("questionary.text")

    context = _context(tmp_path)
    step = BotSetupStep(GlobalState(project_dir=tmp_path, quiet=True), context)

    result = step.run()

    assert result.ok is True
    ask.assert_not_called()
    update_env.assert_not_called()


def test_bot_setup_step_reads_tokens_from_the_toml_when_unattended(
    tmp_path: Path, mocker
) -> None:
    """Under `--unattended` there is nobody to ask: the tokens, if known in
    advance, come from the same axion.toml as everything else."""
    from axion_wizard.steps.s08b_bot_setup import BotSetupStep

    update_env, deploy, _wait = _mock_apply_targets(mocker)
    config_path = tmp_path / "axion.toml"
    config_path.write_text(
        'mm_bot_token = "bot-from-toml"\nmm_webhook_token = "hook-from-toml"\n',
        encoding="utf-8",
    )

    context = _context(tmp_path)
    step = BotSetupStep(
        GlobalState(project_dir=tmp_path, quiet=True, unattended=True, config_path=config_path),
        context,
    )

    result = step.run()

    assert result.ok is True
    update_env.assert_any_call(tmp_path / ".env", "MM_BOT_TOKEN", "bot-from-toml")
    update_env.assert_any_call(tmp_path / ".env", "MM_WEBHOOK_TOKEN", "hook-from-toml")
    deploy.assert_called_once()


def test_bot_setup_step_asks_thread_preference_only_when_theres_a_bot_token(
    tmp_path: Path, mocker
) -> None:
    """With no bot there is no async mode, and with no async mode this setting
    has no effect at all — it should not even be asked about."""
    from axion_wizard.steps.s08b_bot_setup import BotSetupStep

    _mock_apply_targets(mocker)
    mocker.patch(
        "axion_wizard.steps.s08b_bot_setup.interactive_input_available", return_value=True
    )
    ask = mocker.patch("questionary.text")
    ask.return_value.ask.side_effect = ["", "webhook-token-456"]
    confirm = mocker.patch("questionary.confirm")

    context = _context(tmp_path)
    step = BotSetupStep(GlobalState(project_dir=tmp_path, quiet=True), context)
    step.run()

    confirm.assert_not_called()


def test_bot_setup_step_writes_the_thread_preference_when_confirmed(
    tmp_path: Path, mocker
) -> None:
    from axion_wizard.steps.s08b_bot_setup import BotSetupStep

    update_env, _deploy, _wait = _mock_apply_targets(mocker)
    mocker.patch(
        "axion_wizard.steps.s08b_bot_setup.interactive_input_available", return_value=True
    )
    ask = mocker.patch("questionary.text")
    ask.return_value.ask.side_effect = ["bot-token-123", ""]
    mocker.patch("questionary.confirm").return_value.ask.return_value = True

    context = _context(tmp_path)
    step = BotSetupStep(GlobalState(project_dir=tmp_path, quiet=True), context)
    step.run()

    update_env.assert_any_call(tmp_path / ".env", "AI_REPLY_IN_THREAD", "true")


def test_bot_setup_step_writes_the_thread_preference_when_declined(
    tmp_path: Path, mocker
) -> None:
    """Choosing "no" must be written too: not writing it would leave the
    default (in-thread) in place and the "no" answer would have no effect."""
    from axion_wizard.steps.s08b_bot_setup import BotSetupStep

    update_env, _deploy, _wait = _mock_apply_targets(mocker)
    mocker.patch(
        "axion_wizard.steps.s08b_bot_setup.interactive_input_available", return_value=True
    )
    ask = mocker.patch("questionary.text")
    ask.return_value.ask.side_effect = ["bot-token-123", ""]
    mocker.patch("questionary.confirm").return_value.ask.return_value = False

    context = _context(tmp_path)
    step = BotSetupStep(GlobalState(project_dir=tmp_path, quiet=True), context)
    step.run()

    update_env.assert_any_call(tmp_path / ".env", "AI_REPLY_IN_THREAD", "false")


def test_bot_setup_step_reads_thread_preference_from_the_toml_when_unattended(
    tmp_path: Path, mocker
) -> None:
    from axion_wizard.steps.s08b_bot_setup import BotSetupStep

    update_env, _deploy, _wait = _mock_apply_targets(mocker)
    config_path = tmp_path / "axion.toml"
    config_path.write_text(
        'mm_bot_token = "bot-from-toml"\nai_reply_in_thread = false\n', encoding="utf-8"
    )

    context = _context(tmp_path)
    step = BotSetupStep(
        GlobalState(project_dir=tmp_path, quiet=True, unattended=True, config_path=config_path),
        context,
    )
    step.run()

    update_env.assert_any_call(tmp_path / ".env", "AI_REPLY_IN_THREAD", "false")


def test_bot_setup_step_unattended_bot_token_without_thread_preference_leaves_default(
    tmp_path: Path, mocker
) -> None:
    """With no `ai_reply_in_thread` in axion.toml there is nowhere to get an
    answer from: nothing is forced, and `.env` keeps the value it already
    had."""
    from axion_wizard.steps.s08b_bot_setup import BotSetupStep

    update_env, _deploy, _wait = _mock_apply_targets(mocker)
    config_path = tmp_path / "axion.toml"
    config_path.write_text('mm_bot_token = "bot-from-toml"\n', encoding="utf-8")

    context = _context(tmp_path)
    step = BotSetupStep(
        GlobalState(project_dir=tmp_path, quiet=True, unattended=True, config_path=config_path),
        context,
    )
    step.run()

    written_keys = {call.args[1] for call in update_env.call_args_list}
    assert "AI_REPLY_IN_THREAD" not in written_keys


def test_bot_setup_step_unattended_without_tokens_in_the_toml_just_skips(
    tmp_path: Path, mocker
) -> None:
    from axion_wizard.steps.s08b_bot_setup import BotSetupStep

    update_env, _deploy, _wait = _mock_apply_targets(mocker)
    config_path = tmp_path / "axion.toml"
    config_path.write_text('host = "192.168.1.50"\n', encoding="utf-8")

    context = _context(tmp_path)
    step = BotSetupStep(
        GlobalState(project_dir=tmp_path, quiet=True, unattended=True, config_path=config_path),
        context,
    )

    result = step.run()

    assert result.ok is True
    update_env.assert_not_called()


def test_bot_setup_step_rejects_a_token_with_a_forbidden_character(
    tmp_path: Path, mocker
) -> None:
    """A token with `$` would break Compose's interpolation in `.env` (§9) — it
    is discarded with a warning instead of being written as-is."""
    from axion_wizard.steps.s08b_bot_setup import BotSetupStep

    update_env, _deploy, _wait = _mock_apply_targets(mocker)
    mocker.patch(
        "axion_wizard.steps.s08b_bot_setup.interactive_input_available", return_value=True
    )
    ask = mocker.patch("questionary.text")
    ask.return_value.ask.side_effect = ["token$conguion", ""]

    context = _context(tmp_path)
    step = BotSetupStep(GlobalState(project_dir=tmp_path, quiet=True), context)

    result = step.run()

    assert result.ok is True
    update_env.assert_not_called()


def test_bot_setup_step_dry_run_touches_nothing(tmp_path: Path, mocker) -> None:
    from axion_wizard.steps.s08b_bot_setup import BotSetupStep

    update_env, deploy, _wait = _mock_apply_targets(mocker)
    ask = mocker.patch("questionary.text")

    context = _context(tmp_path)
    step = BotSetupStep(GlobalState(project_dir=tmp_path, dry_run=True), context)

    result = step.run()

    assert result.ok is True
    ask.assert_not_called()
    update_env.assert_not_called()
    deploy.assert_not_called()


# --- interactivity guards ------------------------------------------------------------


def test_config_step_fails_readably_without_a_terminal(tmp_path: Path, mocker) -> None:
    """With no console, questionary raises `NoConsoleScreenBufferError` and the
    error surfaced as "Unexpected error: No Windows console found" — raw, and
    exactly what §8 forbids. It must be a ConfigError that says what to do."""
    from axion_wizard.errors import ConfigError
    from axion_wizard.steps.s03_config import ConfigStep

    mocker.patch("axion_wizard.steps.prompts.interactive_input_available", return_value=False)
    context = _context(tmp_path)
    context.config = None
    step = ConfigStep(GlobalState(project_dir=tmp_path), context)

    with pytest.raises(ConfigError, match="interactive terminal") as excinfo:
        step.run()
    assert any("--unattended" in action for action in excinfo.value.steps)


def test_network_step_skips_the_cgnat_question_without_a_terminal(
    tmp_path: Path, mocker
) -> None:
    """Asking for the router's WAN IP with nobody in front would hang the step."""
    from axion_wizard.steps.context import NetworkFacts
    from axion_wizard.steps.s02_network import NetworkStep

    mocker.patch(
        "axion_wizard.steps.s02_network.interactive_input_available", return_value=False
    )
    ask = mocker.patch.object(NetworkStep, "_ask_router_wan_ip")
    context = _context(tmp_path)
    step = NetworkStep(GlobalState(project_dir=tmp_path, quiet=True), context)

    facts = NetworkFacts(public_ip="203.0.113.45")
    step._check_cgnat(facts)

    ask.assert_not_called()
    assert facts.cgnat is False


def test_wireguard_step_works_unattended(tmp_path: Path, mocker) -> None:
    """With no terminal and no prompts, the initial client is created all the
    same: that is the practical difference between holding the password in
    plaintext and holding a hash."""
    from axion_wizard.steps.s08_wireguard import WireguardStep

    mocker.patch("axion_wizard.steps.s08_wireguard.wg.wait_for_panel_ready")
    panel = mocker.MagicMock()
    panel.__aenter__ = mocker.AsyncMock(return_value=panel)
    panel.__aexit__ = mocker.AsyncMock(return_value=False)
    panel.login = mocker.AsyncMock()
    mocker.patch(
        "axion_wizard.steps.s08_wireguard.wg.WireguardPanelClient", return_value=panel
    )
    mocker.patch(
        "axion_wizard.steps.s08_wireguard.wg.create_client_with_qr",
        new=mocker.AsyncMock(
            return_value=SimpleNamespace(id="1", name="primer-cliente", config_text="[i]")
        ),
    )
    mocker.patch("axion_wizard.steps.s08_wireguard.wg.render_qr_terminal", return_value="")

    context = _context(tmp_path)
    step = WireguardStep(
        GlobalState(project_dir=tmp_path, unattended=True, quiet=True), context
    )

    assert step.run().ok is True


def test_wireguard_step_reports_why_not_just_what_on_failure(tmp_path: Path, mocker) -> None:
    """A real regression: `AxionError.__str__` returns only `what`, so
    printing `exc` alone always said the same generic 'could not create
    client' sentence, whatever the real cause — a rejected password, a
    validation error naming the exact field, the panel down mid-request.
    `why` is the one that actually distinguishes them, and it went straight
    into the void."""
    from axion_wizard.steps.s08_wireguard import WireguardStep

    mocker.patch("axion_wizard.steps.s08_wireguard.wg.wait_for_panel_ready")
    panel = mocker.MagicMock()
    panel.__aenter__ = mocker.AsyncMock(return_value=panel)
    panel.__aexit__ = mocker.AsyncMock(return_value=False)
    panel.login = mocker.AsyncMock()
    mocker.patch(
        "axion_wizard.steps.s08_wireguard.wg.WireguardPanelClient", return_value=panel
    )
    mocker.patch(
        "axion_wizard.steps.s08_wireguard.wg.create_client_with_qr",
        new=mocker.AsyncMock(
            side_effect=DeploymentError(
                what="The WireGuard panel could not create client 'first-client'",
                why="expiresAt is required",
                steps=["Read the wireguard container's logs."],
            )
        ),
    )

    context = _context(tmp_path)
    step = WireguardStep(GlobalState(project_dir=tmp_path, unattended=True, quiet=True), context)
    result = step.run()

    assert result.ok is True
    assert "expiresAt is required" in context.warnings[0]
