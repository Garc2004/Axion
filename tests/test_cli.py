from pathlib import Path

import pytest
from typer.testing import CliRunner

from axion_wizard import __version__
from axion_wizard.cli import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


# --- default project directory ------------------------------------------------
#
# Without --project-dir, running the binary exactly as downloaded (a double
# click from ~/Downloads, say) wrote docker-compose.yml, .env, nginx/… loose in
# there. A real incident, not a hypothetical one.


def test_default_project_dir_is_a_subfolder_when_cwd_has_no_deployment(
    tmp_path, monkeypatch
) -> None:
    from axion_wizard.cli import _default_project_dir

    monkeypatch.chdir(tmp_path)
    assert _default_project_dir() == tmp_path / "axion"


def test_default_project_dir_is_cwd_itself_when_a_deployment_already_exists(
    tmp_path, monkeypatch
) -> None:
    """Anyone who followed the README's advice — create a dedicated folder,
    put the binary inside, run it there — must not get one extra level of
    nesting."""
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    monkeypatch.chdir(tmp_path)

    from axion_wizard.cli import _default_project_dir

    assert _default_project_dir() == tmp_path


def test_running_without_project_dir_never_writes_into_the_bare_cwd(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert str(tmp_path / "axion" / "docker-compose.yml") in result.stderr
    assert not (tmp_path / "docker-compose.yml").exists()


def test_doctor_fails_gracefully_without_a_deployed_stack(tmp_path) -> None:
    result = runner.invoke(app, ["--project-dir", str(tmp_path), "doctor"])
    assert result.exit_code == 1
    assert "docker-compose.yml" in result.stderr


def test_doctor_reports_ok_against_a_healthy_stack(tmp_path, mocker) -> None:
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  wireguard:\n    image: x\n"
    )
    (tmp_path / ".env").write_text("OLLAMA_MODEL=qwen2.5:1.5b\n")
    (tmp_path / "wg.env").write_text("WG_HOST=192.168.1.50\n")

    from axion_wizard.steps.s09_verify import CheckResult

    mocker.patch(
        "axion_wizard.steps.s09_verify.run_all_checks",
        mocker.AsyncMock(
            return_value=[CheckResult("Containers healthy", True, "3 services OK")]
        ),
    )

    result = runner.invoke(app, ["--project-dir", str(tmp_path), "doctor"])
    assert result.exit_code == 0
    assert "Containers healthy" in result.stdout


def test_doctor_exits_nonzero_when_a_check_fails(tmp_path, mocker) -> None:
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  wireguard:\n    image: x\n"
    )
    (tmp_path / ".env").write_text("OLLAMA_MODEL=qwen2.5:1.5b\n")
    (tmp_path / "wg.env").write_text("WG_HOST=192.168.1.50\n")

    from axion_wizard.steps.s09_verify import CheckResult

    mocker.patch(
        "axion_wizard.steps.s09_verify.run_all_checks",
        mocker.AsyncMock(return_value=[CheckResult("HTTPS responds", False, "connection refused")]),
    )

    result = runner.invoke(app, ["--project-dir", str(tmp_path), "doctor"])
    assert result.exit_code == 1
    assert "HTTPS responds" in result.stdout


def _stub_deployment(tmp_path) -> None:
    (tmp_path / "docker-compose.yml").write_text("services:\n  wireguard:\n    image: x\n")
    (tmp_path / ".env").write_text("OLLAMA_MODEL=qwen2.5:1.5b\n")
    (tmp_path / "wg.env").write_text("WG_HOST=192.168.1.50\n")


def test_doctor_does_not_request_elevation(tmp_path, mocker) -> None:
    """`doctor` only reads: asking for UAC/sudo to diagnose would be noise."""
    _stub_deployment(tmp_path)
    mocker.patch("axion_wizard.cli.privileges.is_elevated", return_value=False)
    relaunch = mocker.patch("axion_wizard.cli.privileges.relaunch_elevated", return_value=0)

    from axion_wizard.steps.s09_verify import CheckResult

    mocker.patch(
        "axion_wizard.steps.s09_verify.run_all_checks",
        mocker.AsyncMock(return_value=[CheckResult("Containers healthy", True, "ok")]),
    )

    runner.invoke(app, ["--project-dir", str(tmp_path), "doctor"])
    relaunch.assert_not_called()


@pytest.mark.parametrize(
    "argv",
    [
        ["gen-cert", "192.168.1.50"],
        ["models"],
        ["models", "pull", "qwen2.5:1.5b"],
        ["wireguard", "add-client", "mi-telefono"],
        ["network-check"],
        ["logs"],
    ],
)
def test_commands_that_touch_nothing_privileged_do_not_elevate(argv, mocker) -> None:
    """Regression: with a denylist, every new subcommand inherited elevation
    by accident. `gen-cert` only writes a couple of files and still triggered
    UAC."""
    mocker.patch("axion_wizard.cli.privileges.is_elevated", return_value=False)
    relaunch = mocker.patch("axion_wizard.cli.privileges.relaunch_elevated", return_value=0)

    runner.invoke(app, argv)
    relaunch.assert_not_called()


@pytest.mark.parametrize(
    "argv",
    [["install"], ["up"], ["down"], ["uninstall"], ["set-webhook-token", "sometoken12345"]],
)
def test_commands_that_change_the_system_do_elevate(argv, mocker) -> None:
    mocker.patch("axion_wizard.cli.privileges.is_elevated", return_value=False)
    relaunch = mocker.patch("axion_wizard.cli.privileges.relaunch_elevated", return_value=0)

    runner.invoke(app, argv)
    relaunch.assert_called_once()


def test_default_flow_without_subcommand_elevates(mocker) -> None:
    mocker.patch("axion_wizard.cli.privileges.is_elevated", return_value=False)
    relaunch = mocker.patch("axion_wizard.cli.privileges.relaunch_elevated", return_value=0)

    runner.invoke(app, [])
    relaunch.assert_called_once()


def test_install_requests_elevation_when_not_elevated(mocker) -> None:
    mocker.patch("axion_wizard.cli.privileges.is_elevated", return_value=False)
    relaunch = mocker.patch("axion_wizard.cli.privileges.relaunch_elevated", return_value=0)

    result = runner.invoke(app, ["install"])

    relaunch.assert_called_once()
    assert result.exit_code == 0


def test_install_does_not_request_elevation_when_already_elevated(mocker) -> None:
    mocker.patch("axion_wizard.cli.privileges.is_elevated", return_value=True)
    relaunch = mocker.patch("axion_wizard.cli.privileges.relaunch_elevated", return_value=0)

    runner.invoke(app, ["install"])
    relaunch.assert_not_called()


def test_no_elevate_flag_skips_elevation(mocker) -> None:
    mocker.patch("axion_wizard.cli.privileges.is_elevated", return_value=False)
    relaunch = mocker.patch("axion_wizard.cli.privileges.relaunch_elevated", return_value=0)

    runner.invoke(app, ["--no-elevate", "install"])
    relaunch.assert_not_called()


def test_dry_run_skips_elevation(mocker) -> None:
    """`--dry-run` touches nothing, so it needs no privileges."""
    mocker.patch("axion_wizard.cli.privileges.is_elevated", return_value=False)
    relaunch = mocker.patch("axion_wizard.cli.privileges.relaunch_elevated", return_value=0)

    runner.invoke(app, ["--dry-run", "install"])
    relaunch.assert_not_called()


def test_elevation_failure_reports_actionable_error(mocker) -> None:
    from axion_wizard.privileges import ElevationError

    mocker.patch("axion_wizard.cli.privileges.is_elevated", return_value=False)
    mocker.patch(
        "axion_wizard.cli.privileges.relaunch_elevated",
        side_effect=ElevationError("the user cancelled the UAC prompt"),
    )

    result = runner.invoke(app, ["install"])
    assert result.exit_code == 1
    assert "dministrator" in result.stderr


def test_elevation_propagates_child_exit_code(mocker) -> None:
    mocker.patch("axion_wizard.cli.privileges.is_elevated", return_value=False)
    mocker.patch("axion_wizard.cli.privileges.relaunch_elevated", return_value=7)

    result = runner.invoke(app, ["install"])
    assert result.exit_code == 7


def test_elevation_pins_the_project_dir_for_the_child(tmp_path, mocker) -> None:
    """A process launched by UAC starts in C:\\Windows\\System32, not in the
    parent's directory. Without being passed `--project-dir` explicitly, the
    child would fall back to its default (`Path.cwd()`) and deploy the stack
    in there."""
    mocker.patch("axion_wizard.cli.privileges.is_elevated", return_value=False)
    relaunch = mocker.patch("axion_wizard.cli.privileges.relaunch_elevated", return_value=0)

    runner.invoke(app, ["--project-dir", str(tmp_path), "install"])

    kwargs = relaunch.call_args.kwargs
    expected = str(tmp_path.resolve())
    assert kwargs["leading_args"] == ["--project-dir", expected]
    assert kwargs["working_dir"] == expected


def test_elevation_resolves_a_relative_project_dir(tmp_path, monkeypatch, mocker) -> None:
    """A relative path would resolve against a DIFFERENT directory in the child."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "axion").mkdir()
    mocker.patch("axion_wizard.cli.privileges.is_elevated", return_value=False)
    relaunch = mocker.patch("axion_wizard.cli.privileges.relaunch_elevated", return_value=0)

    runner.invoke(app, ["--project-dir", "axion", "install"])

    passed = Path(relaunch.call_args.kwargs["working_dir"])
    assert passed.is_absolute()
    assert passed == (tmp_path / "axion").resolve()


@pytest.mark.parametrize(
    "argv", [["install", "--help"], ["up", "--help"], ["uninstall", "--help"]]
)
def test_asking_for_help_never_requests_elevation(argv, mocker) -> None:
    """Click runs the group callback before processing a subcommand's
    `--help`: without the guard, reading `install`'s help opened a UAC prompt
    and — since it waits for the elevated process — left the command
    blocked."""
    mocker.patch("axion_wizard.cli.privileges.is_elevated", return_value=False)
    relaunch = mocker.patch("axion_wizard.cli.privileges.relaunch_elevated", return_value=0)

    result = runner.invoke(app, argv)

    relaunch.assert_not_called()
    assert result.exit_code == 0
    assert "Usage" in result.stdout or "Uso" in result.stdout


def test_parent_does_not_also_pause_after_relaunching(mocker) -> None:
    """The elevated child pauses in its own window; if the parent paused too,
    a single run would ask for Enter twice in two windows."""
    mocker.patch("axion_wizard.cli.privileges.is_elevated", return_value=False)
    mocker.patch("axion_wizard.cli.privileges.relaunch_elevated", return_value=0)
    disable = mocker.patch("axion_wizard.cli.winconsole.disable_pause")

    runner.invoke(app, ["install"])
    disable.assert_called_once()


def test_help_lists_all_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    commands = [
        "install",
        "doctor",
        "network-check",
        "gen-cert",
        "set-webhook-token",
        "up",
        "down",
        "logs",
        "uninstall",
        "models",
        "wireguard",
    ]
    for cmd in commands:
        assert cmd in result.stdout
