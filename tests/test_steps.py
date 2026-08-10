"""Tests de los pasos individuales del flujo de instalación."""

from pathlib import Path

import pytest

from axion_wizard.cli import GlobalState
from axion_wizard.detect.docker import DockerContextInfo, DockerInfo
from axion_wizard.detect.hardware import HardwareInfo
from axion_wizard.detect.platform import OsInfo, WslInfo
from axion_wizard.domain.config import AccessMode, AxionConfig, WireguardVariant
from axion_wizard.errors import PlatformError
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
        wireguard_admin_password_hash="$2b$12$" + "b" * 53,
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
    """Compose v1 falla más adelante con errores de esquema ilegibles."""
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
    """La salida decisiva de §4.1: de aquí depende todo el resto del flujo."""
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
    context = InstallContext(tmp_path)
    step = EnvironmentStep(GlobalState(project_dir=tmp_path, quiet=True), context)

    result = step.run()

    assert result.data["wireguard_variant"] == "ports"
    assert context.require_environment().wireguard_variant == "ports"


def _env_step(tmp_path: Path, mocker, **docker_kwargs):
    """`EnvironmentStep` con las llamadas de red/hardware/Docker mockeadas,
    lista para invocar los métodos de aviso directamente."""
    from axion_wizard.steps.s01_environment import EnvironmentStep

    mocker.patch(
        "axion_wizard.steps.s01_environment.detect_docker.gather_docker_info",
        return_value=_docker_info(**docker_kwargs),
    )
    context = InstallContext(tmp_path)
    return EnvironmentStep(GlobalState(project_dir=tmp_path, quiet=True), context)


# --- aviso de exposición LAN bajo Docker Desktop en Windows ---------------------------
#
# Regresión de un incidente real: un despliegue con Docker publicando los
# puertos y el firewall bien configurado seguía sin responder desde la LAN
# porque axion-wizard.exe corre nativo en Windows (no dentro de WSL), y el
# aviso existente (`_warn_about_broken_mirrored`) exige `wsl.inside_wsl` —
# nunca se evaluaba en el caso más común.


def test_lan_exposure_warning_skipped_when_inside_wsl(tmp_path: Path, mocker) -> None:
    """Ese caso ya lo cubre `_warn_about_broken_mirrored`; no hay que avisar dos veces."""
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
    """Docker Engine nativo (no Desktop) no tiene este problema — no publica
    a través de una VM WSL2 intermedia."""
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
    """Mirrored activo y red no-Public: la configuración de Windows parece
    correcta. Lo único que queda fuera del alcance del wizard es el
    aislamiento de clientes en el router — se avisa igual, sin poder
    confirmarlo desde aquí."""
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

    # Dos avisos: el aislamiento de clientes del router y, ahora, el bug de
    # stalls TCP del propio mirrored networking (moby/moby#48201) — el que
    # explica que los mensajes solo aparezcan al recargar con F5.
    assert len(step.context.warnings) == 2
    assert "router" in step.context.warnings[0]
    assert "aísle" in step.context.warnings[0] or "isolation" in step.context.warnings[0]
    assert "F5" in step.context.warnings[1]
    assert "48201" in step.context.warnings[1]


# --- passthrough real de GPU, no solo presencia -------------------------------------
#
# Regresión de un incidente real: una GTX 650 (Kepler, 2012) la detecta
# nvidia-smi sin problema, pero Docker no puede pasarla a un contenedor bajo
# WSL2. El compose reservaba la GPU igual para `ollama`, que se quedaba
# parado en `created` para siempre y arrastraba a `fastapi` con él.


def _hardware_with(*gpus):
    from axion_wizard.detect.hardware import HardwareInfo

    return HardwareInfo(
        ram_total_bytes=16 * 1024**3, cpu_logical=8, cpu_physical=4, gpus=list(gpus)
    )


def test_gpu_passthrough_skipped_without_a_gpu(tmp_path: Path, mocker) -> None:
    """Sin GPU no hay nada que probar — y probar igual costaría una
    descarga de imagen innecesaria en la inmensa mayoría de instalaciones."""
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
    """`--gpus` es del runtime de NVIDIA y da negativo siempre en un equipo
    AMD: probarlo así dejaba la GPU sin usar sin que nada lo explicara."""
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
    assert "render" in step.context.warnings[0], "debe decir qué grupos hacen falta"


def test_intel_gpu_says_there_is_no_ollama_image_instead_of_blaming_the_driver(
    tmp_path: Path, mocker
) -> None:
    """Mandar a actualizar el controlador de NVIDIA a quien tiene una Intel
    manda a buscar un problema que no existe: no hay imagen de Ollama para
    Intel, y punto."""
    from axion_wizard.detect.hardware import GpuInfo

    step = _env_step(tmp_path, mocker)

    result = step._check_gpu_passthrough(_hardware_with(GpuInfo(vendor="intel", name="Arc A770")))

    assert result == "none"
    warning = step.context.warnings[0]
    assert "Intel" in warning
    assert "NVIDIA" not in warning


def test_model_prompt_defaults_to_the_model_already_installed(tmp_path: Path, mocker) -> None:
    """Quien hizo `model set qwen2.5:3b` y reinstala no debe perder su
    elección por pulsar Enter: el prompt venía marcado sobre la recomendación
    del catálogo, no sobre lo que hay puesto."""
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
    """El compose no debe pedir la GPU solo porque `nvidia-smi` la vea —
    tiene que estar confirmado que Docker puede usarla de verdad, o
    `ollama` se queda parado en `created` para siempre (§7, incidente real)."""
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
    """La imagen por defecto no trae las bibliotecas de AMD: pasarle
    `/dev/kfd` sin cambiarla deja el modelo en CPU igualmente."""
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
    """§6.2: en /mnt/c el I/O es lento y los permisos POSIX de .env se pierden."""
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

    assert any("filesystem de Windows" in w for w in context.warnings)


# --- paso 4: certificado --------------------------------------------------------------


def test_certificate_step_adds_the_vpn_ip_to_the_san_in_host_variant(tmp_path: Path) -> None:
    """§6.1: con `network_mode: host`, 10.8.0.1 es un IP real del host y los
    clientes de la VPN entran por ahí — el cert debe cubrirlo."""
    from axion_wizard.services import certs
    from axion_wizard.steps.s04_certificate import CertificateStep

    context = _context(tmp_path, WireguardVariant.HOST)
    step = CertificateStep(GlobalState(project_dir=tmp_path, quiet=True), context)

    step.run()

    san = certs.verify_certificate_has_san(step.cert_path)
    assert "IP:192.168.1.50" in san
    assert "IP:10.8.0.1" in san


def test_certificate_step_omits_the_vpn_ip_in_ports_variant(tmp_path: Path) -> None:
    """En Windows/Docker Desktop esa IP solo existe dentro de la VPN."""
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


# --- paso 5: compose y archivos ----------------------------------------------------------


def test_compose_step_writes_the_fastapi_build_context(tmp_path: Path, mocker) -> None:
    """El compose declara `build.context: ./fastapi`; sin estos archivos,
    `up --build` falla con un error que no menciona la causa real."""
    from axion_wizard.steps.s05_compose import ComposeStep

    mocker.patch("axion_wizard.steps.s05_compose.config_validate")
    context = _context(tmp_path)
    ComposeStep(GlobalState(project_dir=tmp_path, quiet=True), context).run()

    for filename in ("Dockerfile", "main.py", "requirements.txt"):
        assert (tmp_path / "fastapi" / filename).exists()


def test_fastapi_requirements_include_python_multipart(tmp_path: Path, mocker) -> None:
    """Regresión real: `main.py` llama a `request.form()` para leer el
    webhook saliente de Mattermost, y Starlette exige `python-multipart`
    para eso — sin ella, CADA llamada al webhook fallaba con 500
    (`AssertionError: The python-multipart library must be installed`),
    de forma determinista en cualquier instalación, no solo en la máquina
    donde se descubrió."""
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
    # §4.5: la variable de SSRF va siempre, sin que el usuario sepa que existe.
    assert "fastapi:8000 fastapi" in compose_text


def test_compose_step_dry_run_writes_nothing(tmp_path: Path, mocker) -> None:
    from axion_wizard.steps.s05_compose import ComposeStep

    validate = mocker.patch("axion_wizard.steps.s05_compose.config_validate")
    context = _context(tmp_path)
    ComposeStep(GlobalState(project_dir=tmp_path, dry_run=True), context).run()

    assert not (tmp_path / "docker-compose.yml").exists()
    assert not (tmp_path / ".env").exists()
    validate.assert_not_called()


# --- paso 8: wireguard -------------------------------------------------------------------


def test_wireguard_step_skips_without_a_password_instead_of_failing(
    tmp_path: Path, mocker
) -> None:
    """El stack ya está levantado: no crear el cliente inicial no es un fallo
    del despliegue, y se puede hacer luego con `wireguard add-client`."""
    from axion_wizard.steps.s08_wireguard import WireguardStep

    context = _context(tmp_path)
    step = WireguardStep(GlobalState(project_dir=tmp_path, quiet=True), context)
    mocker.patch.object(step, "_ask_password", return_value=None)

    result = step.run()

    assert result.ok is True
    assert any("add-client" in w for w in context.warnings)


def test_wireguard_step_does_not_prompt_when_unattended(tmp_path: Path) -> None:
    from axion_wizard.steps.s08_wireguard import WireguardStep

    context = _context(tmp_path)
    step = WireguardStep(GlobalState(project_dir=tmp_path, unattended=True), context)

    assert step._ask_password() is None


# --- paso 9: bot y webhook de Mattermost ---------------------------------------------
#
# No hay forma de crear el bot ni el webhook sin la interfaz web de
# Mattermost (no expone API sin sesión, y la sesión exige una cuenta ya
# creada por un humano) — así que este paso no lo intenta: se detiene y pide
# los tokens. Lo que se prueba aquí es que la escritura y el mensaje final
# sean correctos, no la creación en sí, que no existe.


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
    # Un solo recreate para los tres valores, no uno por cada uno.
    deploy.assert_called_once()
    wait_healthy.assert_called_once()


def test_bot_setup_step_accepts_only_the_bot_token(tmp_path: Path, mocker) -> None:
    """Los dos son independientes: dejar uno en blanco no debe impedir
    aplicar el otro."""
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
    """Dejar los dos en blanco no es un fallo del despliegue: se aplican
    después con set-bot-token/set-webhook-token, igual que siempre."""
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
    """En `--unattended` no hay a quién preguntarle: los tokens, si se
    conocen de antemano, vienen del mismo axion.toml que el resto."""
    from axion_wizard.steps.s08b_bot_setup import BotSetupStep

    update_env, deploy, _wait = _mock_apply_targets(mocker)
    config_path = tmp_path / "axion.toml"
    config_path.write_text(
        'mm_bot_token = "bot-desde-toml"\nmm_webhook_token = "hook-desde-toml"\n',
        encoding="utf-8",
    )

    context = _context(tmp_path)
    step = BotSetupStep(
        GlobalState(project_dir=tmp_path, quiet=True, unattended=True, config_path=config_path),
        context,
    )

    result = step.run()

    assert result.ok is True
    update_env.assert_any_call(tmp_path / ".env", "MM_BOT_TOKEN", "bot-desde-toml")
    update_env.assert_any_call(tmp_path / ".env", "MM_WEBHOOK_TOKEN", "hook-desde-toml")
    deploy.assert_called_once()


def test_bot_setup_step_asks_thread_preference_only_when_theres_a_bot_token(
    tmp_path: Path, mocker
) -> None:
    """Sin bot no hay modo asíncrono, y sin modo asíncrono este ajuste no
    tiene ningún efecto — no debería ni preguntarse."""
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
    """Elegir "no" también debe escribirse: dejarlo sin escribir dejaría el
    valor por defecto (en hilo) sin que la respuesta "no" tuviera efecto."""
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
        'mm_bot_token = "bot-desde-toml"\nai_reply_in_thread = false\n', encoding="utf-8"
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
    """Sin `ai_reply_in_thread` en el axion.toml no hay de dónde sacar una
    respuesta: no se fuerza nada, y `.env` conserva el valor que ya tenía."""
    from axion_wizard.steps.s08b_bot_setup import BotSetupStep

    update_env, _deploy, _wait = _mock_apply_targets(mocker)
    config_path = tmp_path / "axion.toml"
    config_path.write_text('mm_bot_token = "bot-desde-toml"\n', encoding="utf-8")

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
    """Un token con `$` rompería la interpolación de Compose en `.env`
    (§9) — se descarta con un aviso en vez de escribirse tal cual."""
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


# --- guardas de interactividad -------------------------------------------------------


def test_config_step_fails_readably_without_a_terminal(tmp_path: Path, mocker) -> None:
    """Sin consola, questionary lanza `NoConsoleScreenBufferError` y el error
    salía como "Error inesperado: No Windows console found" — crudo, y justo
    lo que §8 prohíbe. Debe ser un ConfigError con qué hacer."""
    from axion_wizard.errors import ConfigError
    from axion_wizard.steps.s03_config import ConfigStep

    mocker.patch("axion_wizard.steps.prompts.interactive_input_available", return_value=False)
    context = _context(tmp_path)
    context.config = None
    step = ConfigStep(GlobalState(project_dir=tmp_path), context)

    with pytest.raises(ConfigError, match="terminal interactiva") as excinfo:
        step.run()
    assert any("--unattended" in action for action in excinfo.value.steps)


def test_network_step_skips_the_cgnat_question_without_a_terminal(
    tmp_path: Path, mocker
) -> None:
    """Preguntar la IP WAN del router sin nadie delante colgaría el paso."""
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


def test_wireguard_step_does_not_prompt_without_a_terminal(tmp_path: Path, mocker) -> None:
    from axion_wizard.steps.s08_wireguard import WireguardStep

    mocker.patch(
        "axion_wizard.steps.s08_wireguard.interactive_input_available", return_value=False
    )
    context = _context(tmp_path)
    step = WireguardStep(GlobalState(project_dir=tmp_path), context)

    assert step._ask_password() is None
