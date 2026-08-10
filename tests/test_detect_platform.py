from pathlib import Path

from axion_wizard.detect import platform as plat
from axion_wizard.utils.shell import CommandNotFoundError, CommandResult


def test_is_inside_wsl_true_via_proc_version(tmp_path: Path) -> None:
    proc_version = tmp_path / "version"
    proc_version.write_text("Linux version 5.15.90.1-microsoft-standard-WSL2")
    assert plat.is_inside_wsl(proc_version_path=proc_version, env={}) is True


def test_is_inside_wsl_false_on_native_linux(tmp_path: Path) -> None:
    proc_version = tmp_path / "version"
    proc_version.write_text("Linux version 6.8.0-generic")
    assert plat.is_inside_wsl(proc_version_path=proc_version, env={}) is False


def test_is_inside_wsl_true_via_env_when_proc_version_missing(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    assert plat.is_inside_wsl(proc_version_path=missing, env={"WSL_DISTRO_NAME": "Ubuntu"}) is True


def test_is_inside_wsl_false_on_windows_no_markers(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    assert plat.is_inside_wsl(proc_version_path=missing, env={}) is False


def test_get_wsl_distro_name() -> None:
    assert plat.get_wsl_distro_name(env={"WSL_DISTRO_NAME": "Ubuntu-22.04"}) == "Ubuntu-22.04"
    assert plat.get_wsl_distro_name(env={}) is None


WSL_L_V_OUTPUT = (
    "  NAME      STATE           VERSION\n"
    "* Ubuntu-22.04    Running         2\n"
    "  docker-desktop  Running         2\n"
)


def test_parse_wsl_list_verbose_default_distro() -> None:
    assert plat.parse_wsl_list_verbose(WSL_L_V_OUTPUT) == 2


def test_parse_wsl_list_verbose_named_distro() -> None:
    assert plat.parse_wsl_list_verbose(WSL_L_V_OUTPUT, distro_name="docker-desktop") == 2


def test_parse_wsl_list_verbose_no_match() -> None:
    assert plat.parse_wsl_list_verbose(WSL_L_V_OUTPUT, distro_name="nonexistent") is None


def test_parse_wsl_list_verbose_strips_null_bytes() -> None:
    dirty = "\x00".join(WSL_L_V_OUTPUT)
    assert plat.parse_wsl_list_verbose(dirty, distro_name="Ubuntu-22.04") == 2


def test_detect_wsl_version_from_wsl_exe(mocker, tmp_path: Path) -> None:
    mocker.patch(
        "axion_wizard.detect.platform.run",
        return_value=CommandResult(args=[], returncode=0, stdout=WSL_L_V_OUTPUT, stderr=""),
    )
    missing_marker = tmp_path / "no-run-wsl"
    version = plat.detect_wsl_version(distro_name="Ubuntu-22.04", run_wsl_marker=missing_marker)
    assert version == 2


def test_detect_wsl_version_fallback_to_marker(mocker, tmp_path: Path) -> None:
    mocker.patch("axion_wizard.detect.platform.run", side_effect=CommandNotFoundError("wsl.exe"))
    marker = tmp_path / "run-wsl"
    marker.mkdir()
    assert plat.detect_wsl_version(run_wsl_marker=marker) == 2


def test_detect_wsl_version_unknown_when_nothing_available(mocker, tmp_path: Path) -> None:
    mocker.patch("axion_wizard.detect.platform.run", side_effect=CommandNotFoundError("wsl.exe"))
    missing_marker = tmp_path / "no-run-wsl"
    assert plat.detect_wsl_version(run_wsl_marker=missing_marker) is None


def test_locate_wslconfig_found(tmp_path: Path) -> None:
    user_dir = tmp_path / "Perseus"
    user_dir.mkdir()
    wslconfig = user_dir / ".wslconfig"
    wslconfig.write_text("[wsl2]\nnetworkingMode=mirrored\n")
    assert plat.locate_wslconfig(mnt_c_users=tmp_path) == wslconfig


def test_locate_wslconfig_not_found(tmp_path: Path) -> None:
    (tmp_path / "Perseus").mkdir()
    assert plat.locate_wslconfig(mnt_c_users=tmp_path) is None


# --- locate_wslconfig_native -----------------------------------------------------------
#
# Distinto de `locate_wslconfig`: ese asume que corremos *dentro* de WSL
# (busca vía /mnt/c). `axion-wizard.exe` normalmente corre nativo en
# Windows, donde /mnt/c no existe — ahí el archivo se busca directamente en
# %UserProfile%.


def test_locate_wslconfig_native_found(tmp_path: Path) -> None:
    wslconfig = tmp_path / ".wslconfig"
    wslconfig.write_text("[wsl2]\nnetworkingMode=mirrored\n")
    assert plat.locate_wslconfig_native(home=tmp_path) == wslconfig


def test_locate_wslconfig_native_not_found(tmp_path: Path) -> None:
    assert plat.locate_wslconfig_native(home=tmp_path) is None


def test_is_mirrored_networking_configured_true(tmp_path: Path) -> None:
    wslconfig = tmp_path / ".wslconfig"
    wslconfig.write_text("[wsl2]\nmemory=8GB\nnetworkingMode=mirrored\n")
    assert plat.is_mirrored_networking_configured(wslconfig) is True


def test_is_mirrored_networking_configured_false_different_section(tmp_path: Path) -> None:
    wslconfig = tmp_path / ".wslconfig"
    wslconfig.write_text("[experimental]\nnetworkingMode=mirrored\n")
    assert plat.is_mirrored_networking_configured(wslconfig) is False


def test_is_mirrored_networking_configured_none_path() -> None:
    assert plat.is_mirrored_networking_configured(None) is False


def test_is_eth0_in_forbidden_range() -> None:
    assert plat.is_eth0_in_forbidden_range("172.20.0.5") is True
    assert plat.is_eth0_in_forbidden_range("192.168.1.50") is False
    assert plat.is_eth0_in_forbidden_range(None) is False
    assert plat.is_eth0_in_forbidden_range("not-an-ip") is False


def test_mirrored_networking_is_active_true(tmp_path: Path) -> None:
    wslconfig = tmp_path / ".wslconfig"
    wslconfig.write_text("[wsl2]\nnetworkingMode=mirrored\n")
    assert plat.mirrored_networking_is_active(wslconfig, "192.168.1.50") is True


def test_mirrored_networking_is_active_false_when_eth0_still_internal(tmp_path: Path) -> None:
    wslconfig = tmp_path / ".wslconfig"
    wslconfig.write_text("[wsl2]\nnetworkingMode=mirrored\n")
    assert plat.mirrored_networking_is_active(wslconfig, "172.20.0.5") is False


def test_is_systemd_active_true(mocker) -> None:
    mocker.patch(
        "axion_wizard.detect.platform.run",
        return_value=CommandResult(args=[], returncode=0, stdout="systemd\n", stderr=""),
    )
    assert plat.is_systemd_active() is True


def test_is_systemd_active_false(mocker) -> None:
    mocker.patch(
        "axion_wizard.detect.platform.run",
        return_value=CommandResult(args=[], returncode=0, stdout="init\n", stderr=""),
    )
    assert plat.is_systemd_active() is False


def test_is_systemd_active_command_missing(mocker) -> None:
    mocker.patch("axion_wizard.detect.platform.run", side_effect=CommandNotFoundError("ps"))
    assert plat.is_systemd_active() is False


def test_decide_wireguard_variant_linux_native() -> None:
    assert plat.decide_wireguard_variant("Linux", docker_context_is_desktop=False) == "host"


def test_decide_wireguard_variant_linux_with_docker_desktop() -> None:
    assert plat.decide_wireguard_variant("Linux", docker_context_is_desktop=True) == "ports"


def test_decide_wireguard_variant_windows() -> None:
    assert plat.decide_wireguard_variant("Windows", docker_context_is_desktop=False) == "ports"


def test_gather_wsl_info_not_inside_wsl(tmp_path: Path) -> None:
    missing = tmp_path / "no-proc-version"
    info = plat.gather_wsl_info(proc_version_path=missing, mnt_c_users=tmp_path, env={})
    assert info.inside_wsl is False
    assert info.distro_name is None


def test_gather_wsl_info_inside_wsl(mocker, tmp_path: Path) -> None:
    proc_version = tmp_path / "version"
    proc_version.write_text("Linux version 5.15-microsoft-standard-WSL2")
    users_dir = tmp_path / "Perseus"
    users_dir.mkdir()
    (users_dir / ".wslconfig").write_text("[wsl2]\nnetworkingMode=mirrored\n")

    mocker.patch(
        "axion_wizard.detect.platform.run",
        return_value=CommandResult(args=[], returncode=0, stdout=WSL_L_V_OUTPUT, stderr=""),
    )
    info = plat.gather_wsl_info(
        proc_version_path=proc_version,
        mnt_c_users=tmp_path,
        env={"WSL_DISTRO_NAME": "Ubuntu-22.04"},
    )
    assert info.inside_wsl is True
    assert info.distro_name == "Ubuntu-22.04"
    assert info.version == 2
    assert info.mirrored_configured is True
