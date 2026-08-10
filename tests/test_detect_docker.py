from axion_wizard.detect import docker as dk
from axion_wizard.utils.shell import CommandNotFoundError, CommandResult, CommandTimeoutError


def test_is_compose_v2_true() -> None:
    assert dk.is_compose_v2("v2.29.7") is True
    assert dk.is_compose_v2("2.29.7") is True


def test_is_compose_v2_false_for_v1() -> None:
    assert dk.is_compose_v2("1.29.2") is False


def test_is_compose_v2_false_for_garbage() -> None:
    assert dk.is_compose_v2("not-a-version") is False


def test_get_docker_version_ok(mocker) -> None:
    mocker.patch(
        "axion_wizard.detect.docker.run",
        return_value=CommandResult(
            args=[], returncode=0, stdout="Docker version 27.3.1, build ce12230\n", stderr=""
        ),
    )
    assert dk.get_docker_version() == "Docker version 27.3.1, build ce12230"


def test_get_docker_version_not_installed(mocker) -> None:
    mocker.patch("axion_wizard.detect.docker.run", side_effect=CommandNotFoundError("docker"))
    assert dk.get_docker_version() is None


def test_get_compose_version_v2(mocker) -> None:
    mocker.patch(
        "axion_wizard.detect.docker.run",
        return_value=CommandResult(args=[], returncode=0, stdout="v2.29.7\n", stderr=""),
    )
    version, is_v2 = dk.get_compose_version()
    assert version == "v2.29.7"
    assert is_v2 is True


def test_get_compose_version_missing(mocker) -> None:
    mocker.patch(
        "axion_wizard.detect.docker.run",
        return_value=CommandResult(args=[], returncode=1, stdout="", stderr="unknown command"),
    )
    version, is_v2 = dk.get_compose_version()
    assert version is None
    assert is_v2 is False


CONTEXT_LS_ARRAY = (
    '[{"Name":"default","Current":false},{"Name":"desktop-linux","Current":true}]'
)
CONTEXT_LS_LINES = (
    '{"Name":"default","Current":false}\n{"Name":"desktop-linux","Current":true}\n'
)


def test_parse_context_ls_array_form() -> None:
    entries = dk.parse_context_ls(CONTEXT_LS_ARRAY)
    names = {e["Name"] for e in entries}
    assert names == {"default", "desktop-linux"}


def test_parse_context_ls_line_form() -> None:
    entries = dk.parse_context_ls(CONTEXT_LS_LINES)
    names = {e["Name"] for e in entries}
    assert names == {"default", "desktop-linux"}


def test_parse_context_ls_empty() -> None:
    assert dk.parse_context_ls("") == []


def test_get_docker_contexts_detects_desktop(mocker) -> None:
    mocker.patch(
        "axion_wizard.detect.docker.run",
        return_value=CommandResult(args=[], returncode=0, stdout=CONTEXT_LS_ARRAY, stderr=""),
    )
    info = dk.get_docker_contexts()
    assert info.active_context == "desktop-linux"
    assert info.is_desktop is True
    assert set(info.contexts) == {"default", "desktop-linux"}


def test_get_docker_contexts_native_engine(mocker) -> None:
    output = '[{"Name":"default","Current":true}]'
    mocker.patch(
        "axion_wizard.detect.docker.run",
        return_value=CommandResult(args=[], returncode=0, stdout=output, stderr=""),
    )
    info = dk.get_docker_contexts()
    assert info.active_context == "default"
    assert info.is_desktop is False


def test_gather_docker_info(mocker) -> None:
    def fake_run(args, timeout=10.0):
        if args[:2] == ["docker", "--version"]:
            return CommandResult(
                args=args, returncode=0, stdout="Docker version 27.3.1\n", stderr=""
            )
        if args[:3] == ["docker", "compose", "version"]:
            return CommandResult(args=args, returncode=0, stdout="v2.29.7\n", stderr="")
        if args[:3] == ["docker", "context", "ls"]:
            return CommandResult(args=args, returncode=0, stdout=CONTEXT_LS_ARRAY, stderr="")
        raise AssertionError(f"unexpected command {args}")

    mocker.patch("axion_wizard.detect.docker.run", side_effect=fake_run)
    info = dk.gather_docker_info()
    assert info.installed is True
    assert info.compose_is_v2 is True
    assert info.context.is_desktop is True


def test_get_docker_version_nonzero_exit(mocker) -> None:
    mocker.patch(
        "axion_wizard.detect.docker.run",
        return_value=CommandResult(args=[], returncode=1, stdout="", stderr="not found"),
    )
    assert dk.get_docker_version() is None


def test_get_compose_version_timeout(mocker) -> None:
    from axion_wizard.utils.shell import CommandTimeoutError

    mocker.patch(
        "axion_wizard.detect.docker.run", side_effect=CommandTimeoutError(["docker"], 10.0)
    )
    version, is_v2 = dk.get_compose_version()
    assert version is None
    assert is_v2 is False


def test_get_compose_version_empty_output(mocker) -> None:
    mocker.patch(
        "axion_wizard.detect.docker.run",
        return_value=CommandResult(args=[], returncode=0, stdout="   \n", stderr=""),
    )
    version, is_v2 = dk.get_compose_version()
    assert version is None
    assert is_v2 is False


def test_parse_context_ls_single_object_form() -> None:
    entries = dk.parse_context_ls('{"Name":"default","Current":true}')
    assert entries == [{"Name": "default", "Current": True}]


def test_parse_context_ls_line_form_skips_blank_and_malformed_lines() -> None:
    output = (
        '{"Name":"default","Current":false}\n\n'
        'not-json\n{"Name":"desktop-linux","Current":true}\n'
    )
    entries = dk.parse_context_ls(output)
    names = {e["Name"] for e in entries}
    assert names == {"default", "desktop-linux"}


def test_get_docker_contexts_command_missing(mocker) -> None:
    mocker.patch("axion_wizard.detect.docker.run", side_effect=CommandNotFoundError("docker"))
    info = dk.get_docker_contexts()
    assert info.active_context is None
    assert info.is_desktop is False


def test_get_docker_contexts_nonzero_exit(mocker) -> None:
    mocker.patch(
        "axion_wizard.detect.docker.run",
        return_value=CommandResult(args=[], returncode=1, stdout="", stderr="boom"),
    )
    info = dk.get_docker_contexts()
    assert info.contexts == []


# --- docker_gpu_passthrough_works -----------------------------------------------------


def test_gpu_passthrough_true_when_container_runs(mocker) -> None:
    mocker.patch(
        "axion_wizard.detect.docker.run",
        return_value=CommandResult(args=[], returncode=0, stdout="", stderr=""),
    )
    assert dk.docker_gpu_passthrough_works() is True


def test_gpu_passthrough_false_on_nvidia_container_cli_error(mocker) -> None:
    """El caso real: nvidia-smi ve la GPU, pero el hook de
    nvidia-container-cli falla al arrancar el contenedor bajo WSL2."""
    mocker.patch(
        "axion_wizard.detect.docker.run",
        return_value=CommandResult(
            args=[],
            returncode=1,
            stdout="",
            stderr=(
                "nvidia-container-cli: initialization error: WSL environment "
                "detected but no adapters were found"
            ),
        ),
    )
    assert dk.docker_gpu_passthrough_works() is False


def test_gpu_passthrough_false_when_docker_is_missing(mocker) -> None:
    mocker.patch("axion_wizard.detect.docker.run", side_effect=CommandNotFoundError("docker"))
    assert dk.docker_gpu_passthrough_works() is False


def test_gpu_passthrough_false_on_timeout(mocker) -> None:
    mocker.patch(
        "axion_wizard.detect.docker.run", side_effect=CommandTimeoutError(["docker"], 60.0)
    )
    assert dk.docker_gpu_passthrough_works() is False
