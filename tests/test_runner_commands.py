from pathlib import Path

import pytest
from typer.testing import CliRunner

from axion_wizard.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _never_elevate(mocker):
    """The tests must never trigger a real UAC/sudo prompt."""
    mocker.patch("axion_wizard.cli.privileges.is_elevated", return_value=True)


def _project_with_compose(tmp_path: Path) -> Path:
    (tmp_path / "docker-compose.yml").write_text("services:\n  wireguard:\n    image: x\n")
    (tmp_path / ".env").write_text("OLLAMA_MODEL=qwen2.5:1.5b\n")
    (tmp_path / "wg.env").write_text("WG_HOST=192.168.1.50\n")
    return tmp_path


# --- gen-cert -------------------------------------------------------------------


def test_gen_cert_writes_certificate_with_verified_san(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--project-dir", str(tmp_path), "gen-cert", "192.168.1.50"])
    assert result.exit_code == 0

    cert = tmp_path / "nginx" / "certs" / "cert.crt"
    key = tmp_path / "nginx" / "certs" / "cert.key"
    assert cert.exists() and key.exists()

    from axion_wizard.services import certs

    assert "IP:192.168.1.50" in certs.verify_certificate_has_san(cert)


def test_gen_cert_restarts_nginx_so_it_serves_the_new_certificate(
    tmp_path: Path, mocker
) -> None:
    """`nginx/certs` is a bind mount: the new file is already inside the
    container, but nginx serves the one it loaded into memory at startup.
    Without the restart, `gen-cert` finished green and the browser went on
    seeing the old certificate."""
    from axion_wizard.services.compose import ContainerStatus
    from axion_wizard.utils.shell import CommandResult

    _project_with_compose(tmp_path)
    mocker.patch(
        "axion_wizard.services.compose.get_service_status",
        return_value=ContainerStatus(
            service="nginx", name="dist-nginx-1", state="running", health=None
        ),
    )
    restart = mocker.patch(
        "axion_wizard.services.compose.restart",
        return_value=CommandResult(args=[], returncode=0, stdout="", stderr=""),
    )

    result = runner.invoke(app, ["--project-dir", str(tmp_path), "gen-cert", "192.168.1.50"])

    assert result.exit_code == 0
    restart.assert_called_once_with(tmp_path / "docker-compose.yml", "nginx")


def test_gen_cert_without_a_stack_does_not_touch_docker(tmp_path: Path, mocker) -> None:
    """Generating the certificate before deploying is legitimate: it will be
    used when the stack comes up."""
    restart = mocker.patch("axion_wizard.services.compose.restart")

    result = runner.invoke(app, ["--project-dir", str(tmp_path), "gen-cert", "192.168.1.50"])

    assert result.exit_code == 0
    restart.assert_not_called()


def test_gen_cert_rejects_empty_host_with_clean_error(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--project-dir", str(tmp_path), "gen-cert", "   "])
    assert result.exit_code == 1
    assert "host" in result.stderr.lower()


def test_gen_cert_dry_run_writes_nothing(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["--project-dir", str(tmp_path), "--dry-run", "gen-cert", "192.168.1.50"]
    )
    assert result.exit_code == 0
    assert not (tmp_path / "nginx").exists(), "--dry-run no debe tocar el sistema"


# --- comandos que exigen un compose existente --------------------------------------


@pytest.mark.parametrize("argv", [["up"], ["down"], ["logs"], ["uninstall"]])
def test_compose_commands_fail_cleanly_without_a_project(argv, tmp_path: Path) -> None:
    result = runner.invoke(app, ["--project-dir", str(tmp_path), *argv])
    assert result.exit_code == 1
    assert "docker-compose.yml" in result.stderr


def test_up_dry_run_does_not_invoke_docker(tmp_path: Path, mocker) -> None:
    _project_with_compose(tmp_path)
    deploy = mocker.patch("axion_wizard.steps.s06_deploy.deploy")
    result = runner.invoke(app, ["--project-dir", str(tmp_path), "--dry-run", "up"])
    assert result.exit_code == 0
    deploy.assert_not_called()


def test_up_verifies_the_effective_wg_easy_tag(tmp_path: Path, mocker) -> None:
    """§6.4: the tag that matters is the running container's, not the one
    left written in the compose file — anyone hand-editing it can end up on
    another major, which configures itself incompatibly without a single error
    in the logs."""
    _project_with_compose(tmp_path)
    mocker.patch("axion_wizard.steps.s06_deploy.deploy")
    mocker.patch("axion_wizard.services.compose.ps", return_value=[])
    verify = mocker.patch("axion_wizard.steps.s06_deploy.verify_wg_easy_tag")

    result = runner.invoke(app, ["--project-dir", str(tmp_path), "up"])

    assert result.exit_code == 0
    verify.assert_called_once()


def test_up_of_another_service_skips_the_wg_easy_check(tmp_path: Path, mocker) -> None:
    _project_with_compose(tmp_path)
    mocker.patch("axion_wizard.steps.s06_deploy.deploy")
    mocker.patch("axion_wizard.services.compose.ps", return_value=[])
    verify = mocker.patch("axion_wizard.steps.s06_deploy.verify_wg_easy_tag")

    runner.invoke(app, ["--project-dir", str(tmp_path), "up", "nginx"])

    verify.assert_not_called()


def test_logs_says_so_when_no_service_returned_anything(tmp_path: Path, mocker) -> None:
    """Without this it printed exactly nothing and exited 0: indistinguishable
    from the command having done nothing at all."""
    _project_with_compose(tmp_path)
    mocker.patch("axion_wizard.services.compose.logs", return_value="")

    result = runner.invoke(app, ["--project-dir", str(tmp_path), "logs"])

    assert result.exit_code == 0
    assert "No service returned any logs" in result.stdout


def test_down_dry_run_does_not_invoke_docker(tmp_path: Path, mocker) -> None:
    _project_with_compose(tmp_path)
    down = mocker.patch("axion_wizard.services.compose.down")
    result = runner.invoke(app, ["--project-dir", str(tmp_path), "--dry-run", "down"])
    assert result.exit_code == 0
    down.assert_not_called()


def test_down_invokes_compose_and_reports_success(tmp_path: Path, mocker) -> None:
    _project_with_compose(tmp_path)
    from axion_wizard.utils.shell import CommandResult

    down = mocker.patch(
        "axion_wizard.services.compose.down",
        return_value=CommandResult(args=[], returncode=0, stdout="", stderr=""),
    )
    result = runner.invoke(app, ["--project-dir", str(tmp_path), "down"])
    assert result.exit_code == 0
    down.assert_called_once()
    assert down.call_args.kwargs.get("volumes", False) is False


def test_down_reports_failure_with_nonzero_exit(tmp_path: Path, mocker) -> None:
    _project_with_compose(tmp_path)
    from axion_wizard.utils.shell import CommandResult

    mocker.patch(
        "axion_wizard.services.compose.down",
        return_value=CommandResult(args=[], returncode=1, stdout="", stderr="boom"),
    )
    result = runner.invoke(app, ["--project-dir", str(tmp_path), "down"])
    assert result.exit_code == 1


# --- uninstall --purge ---------------------------------------------------------------


def test_uninstall_purge_requires_typing_the_project_name(tmp_path: Path, mocker) -> None:
    """§9: --purge deletes unrecoverable data, so it asks for confirmation by
    typing the project's name."""
    project = _project_with_compose(tmp_path)
    down = mocker.patch("axion_wizard.services.compose.down")
    mocker.patch("questionary.text", return_value=mocker.Mock(ask=lambda: "nombre-incorrecto"))

    result = runner.invoke(app, ["--project-dir", str(project), "uninstall", "--purge"])

    assert result.exit_code == 1
    # It must delete nothing if the confirmation does not match.
    down.assert_not_called()


def test_uninstall_purge_proceeds_when_name_matches(tmp_path: Path, mocker) -> None:
    project = _project_with_compose(tmp_path)
    from axion_wizard.utils.shell import CommandResult

    down = mocker.patch(
        "axion_wizard.services.compose.down",
        return_value=CommandResult(args=[], returncode=0, stdout="", stderr=""),
    )
    mocker.patch("questionary.text", return_value=mocker.Mock(ask=lambda: project.resolve().name))

    result = runner.invoke(app, ["--project-dir", str(project), "uninstall", "--purge"])

    assert result.exit_code == 0
    assert down.call_args.kwargs["volumes"] is True


def test_uninstall_purge_with_yes_skips_the_prompt(tmp_path: Path, mocker) -> None:
    project = _project_with_compose(tmp_path)
    from axion_wizard.utils.shell import CommandResult

    down = mocker.patch(
        "axion_wizard.services.compose.down",
        return_value=CommandResult(args=[], returncode=0, stdout="", stderr=""),
    )
    text_prompt = mocker.patch("questionary.text")

    result = runner.invoke(app, ["--project-dir", str(project), "--yes", "uninstall", "--purge"])

    assert result.exit_code == 0
    text_prompt.assert_not_called()
    assert down.call_args.kwargs["volumes"] is True


def test_uninstall_purge_dry_run_neither_prompts_nor_deletes(tmp_path: Path, mocker) -> None:
    """`--dry-run` promises to touch nothing, so there is nothing to confirm.
    Asking anyway meant a non-interactive `--dry-run --purge` sat waiting for
    an answer that never came and exited 1."""
    project = _project_with_compose(tmp_path)
    down = mocker.patch("axion_wizard.services.compose.down")
    text_prompt = mocker.patch("questionary.text")

    result = runner.invoke(
        app, ["--project-dir", str(project), "--dry-run", "uninstall", "--purge"]
    )

    assert result.exit_code == 0
    text_prompt.assert_not_called()
    down.assert_not_called()


def test_uninstall_without_purge_keeps_volumes(tmp_path: Path, mocker) -> None:
    project = _project_with_compose(tmp_path)
    from axion_wizard.utils.shell import CommandResult

    down = mocker.patch(
        "axion_wizard.services.compose.down",
        return_value=CommandResult(args=[], returncode=0, stdout="", stderr=""),
    )
    result = runner.invoke(app, ["--project-dir", str(project), "uninstall"])
    assert result.exit_code == 0
    assert down.call_args.kwargs["volumes"] is False


# --- models ------------------------------------------------------------------------


def test_models_lists_catalog_with_hardware_context(mocker) -> None:
    from axion_wizard.services import ollama

    mocker.patch(
        "axion_wizard.services.ollama.fetch_remote_catalog", mocker.AsyncMock(return_value=None)
    )
    mocker.patch(
        "axion_wizard.services.ollama.list_installed_models", mocker.AsyncMock(return_value=[])
    )

    result = runner.invoke(app, ["models"])
    assert result.exit_code == 0
    assert "Hardware detected" in result.stdout
    # at least one model from the embedded fallback appears in the list
    assert any(m.name.split(":")[0] in result.stdout for m in ollama.get_embedded_catalog())


def test_models_pull_dry_run_downloads_nothing(mocker) -> None:
    pull = mocker.patch("axion_wizard.services.ollama.pull_model")
    result = runner.invoke(app, ["--dry-run", "models", "pull", "qwen2.5:1.5b"])
    assert result.exit_code == 0
    pull.assert_not_called()


# --- network-check -------------------------------------------------------------------


# --- set-webhook-token --------------------------------------------------------------


def test_set_webhook_token_fails_cleanly_without_a_project(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["--project-dir", str(tmp_path), "set-webhook-token", "token-de-ejemplo-no-real-000"]
    )
    assert result.exit_code == 1
    assert "docker-compose.yml" in result.stderr


def test_set_webhook_token_rejects_empty_token(tmp_path: Path) -> None:
    _project_with_compose(tmp_path)
    result = runner.invoke(app, ["--project-dir", str(tmp_path), "set-webhook-token", "   "])
    assert result.exit_code == 1
    assert "empty" in result.stderr.lower()


def test_set_webhook_token_rejects_forbidden_char(tmp_path: Path) -> None:
    _project_with_compose(tmp_path)
    result = runner.invoke(app, ["--project-dir", str(tmp_path), "set-webhook-token", "has$dollar"])
    assert result.exit_code == 1
    assert "variable expansion" in result.stderr


def test_set_webhook_token_dry_run_does_not_write_or_deploy(tmp_path: Path, mocker) -> None:
    project = _project_with_compose(tmp_path)
    deploy = mocker.patch("axion_wizard.steps.s06_deploy.deploy")
    result = runner.invoke(
        app,
        [
            "--project-dir",
            str(project),
            "--dry-run",
            "set-webhook-token",
            "token-de-ejemplo-no-real-000",
        ],
    )
    assert result.exit_code == 0
    deploy.assert_not_called()
    assert "MM_WEBHOOK_TOKEN" not in (project / ".env").read_text()


def test_set_webhook_token_writes_env_and_recreates_fastapi(tmp_path: Path, mocker) -> None:
    project = _project_with_compose(tmp_path)
    deploy = mocker.patch("axion_wizard.steps.s06_deploy.deploy")
    wait_for_healthy = mocker.patch("axion_wizard.steps.s06_deploy.wait_for_healthy")
    mocker.patch("axion_wizard.services.compose.ps", return_value=[])

    result = runner.invoke(
        app, ["--project-dir", str(project), "set-webhook-token", "token-de-ejemplo-no-real-000"]
    )

    assert result.exit_code == 0
    assert "MM_WEBHOOK_TOKEN=token-de-ejemplo-no-real-000" in (project / ".env").read_text()
    deploy.assert_called_once()
    assert deploy.call_args.kwargs["services"] == ["fastapi"]
    wait_for_healthy.assert_called_once()
    assert wait_for_healthy.call_args.kwargs["services"] == ["fastapi"]
    # the token must never appear in the terminal output (§9).
    assert "token-de-ejemplo-no-real-000" not in result.stdout


def test_set_webhook_token_preserves_existing_env_lines(tmp_path: Path, mocker) -> None:
    project = _project_with_compose(tmp_path)
    mocker.patch("axion_wizard.steps.s06_deploy.deploy")
    mocker.patch("axion_wizard.steps.s06_deploy.wait_for_healthy")
    mocker.patch("axion_wizard.services.compose.ps", return_value=[])

    runner.invoke(app, ["--project-dir", str(project), "set-webhook-token", "primer-token-valido"])
    runner.invoke(app, ["--project-dir", str(project), "set-webhook-token", "segundo-token-valido"])

    env_text = (project / ".env").read_text()
    assert "OLLAMA_MODEL=qwen2.5:1.5b" in env_text
    assert "MM_WEBHOOK_TOKEN=segundo-token-valido" in env_text
    assert "primer-token-valido" not in env_text
    assert env_text.count("MM_WEBHOOK_TOKEN=") == 1


def test_set_webhook_token_strips_surrounding_whitespace(tmp_path: Path, mocker) -> None:
    project = _project_with_compose(tmp_path)
    mocker.patch("axion_wizard.steps.s06_deploy.deploy")
    mocker.patch("axion_wizard.steps.s06_deploy.wait_for_healthy")
    mocker.patch("axion_wizard.services.compose.ps", return_value=[])

    result = runner.invoke(
        app, ["--project-dir", str(project), "set-webhook-token", "  token-con-espacios  "]
    )

    assert result.exit_code == 0
    assert "MM_WEBHOOK_TOKEN=token-con-espacios\n" in (project / ".env").read_text()


def test_network_check_renders_a_table(mocker) -> None:
    from axion_wizard.detect import network as net

    mocker.patch(
        "axion_wizard.detect.network.get_primary_interface",
        return_value=net.InterfaceInfo(name="eth0", ip="192.168.1.50", mac="aa:bb"),
    )
    mocker.patch(
        "axion_wizard.detect.network.check_ports_psutil",
        return_value=[net.PortStatus(port=51820, protocol="udp", in_use=False)],
    )
    mocker.patch(
        "axion_wizard.detect.network.get_public_ipv4", mocker.AsyncMock(return_value="203.0.113.5")
    )
    mocker.patch(
        "axion_wizard.detect.network.check_connectivity",
        mocker.AsyncMock(return_value={"registry-1.docker.io": True}),
    )

    result = runner.invoke(app, ["network-check"])
    assert result.exit_code == 0
    assert "eth0" in result.stdout
    assert "51820" in result.stdout


# --- model: edit the AI without touching .env or memorising compose commands ------
#
# Changing the model took three manual steps and knowing all three: pull it,
# hand-edit OLLAMA_MODEL, and recreate fastapi with the right `docker compose`
# invocation (a `restart` will not do: environment variables are fixed when the
# container is created). Forgetting the third left the user staring at an AI
# still answering with the old model, with no error.


@pytest.fixture
def _fake_deploy(mocker):
    return {
        "deploy": mocker.patch("axion_wizard.steps.s06_deploy.deploy"),
        "wait": mocker.patch("axion_wizard.steps.s06_deploy.wait_for_healthy"),
    }


def test_model_show_reports_the_current_settings(tmp_path: Path) -> None:
    project = _project_with_compose(tmp_path)
    (project / ".env").write_text(
        "OLLAMA_MODEL=llama3.2:3b\nOLLAMA_SYSTEM_PROMPT=Answer in English.\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["--project-dir", str(project), "model"])
    assert result.exit_code == 0
    assert "llama3.2:3b" in result.stdout
    assert "Answer in English." in result.stdout


def test_model_show_fails_cleanly_without_a_project(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--project-dir", str(tmp_path), "model"])
    assert result.exit_code == 1
    assert "docker-compose.yml" in result.stderr


def test_model_set_writes_env_and_recreates_fastapi(tmp_path: Path, mocker, _fake_deploy) -> None:
    project = _project_with_compose(tmp_path)
    mocker.patch(
        "axion_wizard.services.ollama.list_installed_models",
        mocker.AsyncMock(return_value=[{"name": "llama3.2:3b"}]),
    )

    result = runner.invoke(app, ["--project-dir", str(project), "model", "set", "llama3.2:3b"])

    assert result.exit_code == 0
    assert "OLLAMA_MODEL=llama3.2:3b" in (project / ".env").read_text()
    _fake_deploy["deploy"].assert_called_once()
    _fake_deploy["wait"].assert_called_once()


def test_model_set_pulls_the_model_when_it_is_missing(
    tmp_path: Path, mocker, _fake_deploy
) -> None:
    """Without the pull, the failure arrives later and elsewhere: fastapi
    starts with a model Ollama does not have and every message returns an error
    that mentions the missing download nowhere."""
    project = _project_with_compose(tmp_path)
    mocker.patch(
        "axion_wizard.services.ollama.list_installed_models",
        mocker.AsyncMock(return_value=[]),
    )
    pull = mocker.patch("axion_wizard.services.ollama.pull_model", mocker.AsyncMock())

    result = runner.invoke(app, ["--project-dir", str(project), "model", "set", "llama3.2:3b"])

    assert result.exit_code == 0
    pull.assert_called_once()
    assert "OLLAMA_MODEL=llama3.2:3b" in (project / ".env").read_text()


def test_model_set_no_pull_skips_the_download(tmp_path: Path, mocker, _fake_deploy) -> None:
    project = _project_with_compose(tmp_path)
    mocker.patch(
        "axion_wizard.services.ollama.list_installed_models",
        mocker.AsyncMock(return_value=[]),
    )
    pull = mocker.patch("axion_wizard.services.ollama.pull_model", mocker.AsyncMock())

    result = runner.invoke(
        app, ["--project-dir", str(project), "model", "set", "llama3.2:3b", "--no-pull"]
    )

    assert result.exit_code == 0
    pull.assert_not_called()


def test_model_set_preserves_the_rest_of_the_env(tmp_path: Path, mocker, _fake_deploy) -> None:
    project = _project_with_compose(tmp_path)
    (project / ".env").write_text(
        "POSTGRES_PASSWORD=secreto123\nOLLAMA_MODEL=viejo:1b\nMM_WEBHOOK_TOKEN=tok3n\n"
    )
    mocker.patch(
        "axion_wizard.services.ollama.list_installed_models",
        mocker.AsyncMock(return_value=[{"name": "nuevo:3b"}]),
    )

    runner.invoke(app, ["--project-dir", str(project), "model", "set", "nuevo:3b"])

    env_text = (project / ".env").read_text()
    assert "POSTGRES_PASSWORD=secreto123" in env_text
    assert "MM_WEBHOOK_TOKEN=tok3n" in env_text
    assert "OLLAMA_MODEL=nuevo:3b" in env_text
    assert "viejo:1b" not in env_text


def test_model_set_dry_run_writes_nothing(tmp_path: Path, mocker, _fake_deploy) -> None:
    project = _project_with_compose(tmp_path)
    result = runner.invoke(
        app, ["--project-dir", str(project), "--dry-run", "model", "set", "llama3.2:3b"]
    )
    assert result.exit_code == 0
    _fake_deploy["deploy"].assert_not_called()
    assert "llama3.2:3b" not in (project / ".env").read_text()


def test_model_prompt_writes_the_system_prompt(tmp_path: Path, _fake_deploy) -> None:
    project = _project_with_compose(tmp_path)
    result = runner.invoke(
        app,
        ["--project-dir", str(project), "model", "prompt", "Eres el asistente de AXION."],
    )
    assert result.exit_code == 0
    assert "OLLAMA_SYSTEM_PROMPT=Eres el asistente de AXION." in (project / ".env").read_text()
    _fake_deploy["deploy"].assert_called_once()


def test_model_prompt_accepts_an_empty_string_to_clear_it(tmp_path: Path, _fake_deploy) -> None:
    project = _project_with_compose(tmp_path)
    (project / ".env").write_text("OLLAMA_SYSTEM_PROMPT=algo viejo\n")

    result = runner.invoke(app, ["--project-dir", str(project), "model", "prompt", ""])

    assert result.exit_code == 0
    assert "OLLAMA_SYSTEM_PROMPT=\n" in (project / ".env").read_text()


def test_model_prompt_rejects_a_character_that_would_break_the_env(tmp_path: Path) -> None:
    project = _project_with_compose(tmp_path)
    result = runner.invoke(
        app, ["--project-dir", str(project), "model", "prompt", "usa $VARIABLE"]
    )
    assert result.exit_code == 1
    assert "variable expansion" in result.stderr


def test_model_set_rejects_an_empty_name(tmp_path: Path) -> None:
    project = _project_with_compose(tmp_path)
    result = runner.invoke(app, ["--project-dir", str(project), "model", "set", "   "])
    assert result.exit_code == 1
    assert "model" in result.stderr.lower()


# --- set-bot-token: the asynchronous-mode switch ------------------------------------
#
# Without a bot token, Mattermost abandons the webhook request after ~30s and
# a slow model's answer is lost whole, with no visible error. With one, the
# bridge answers instantly and posts when the model finishes.


def test_set_bot_token_writes_env_and_recreates_fastapi(tmp_path: Path, mocker) -> None:
    project = _project_with_compose(tmp_path)
    deploy = mocker.patch("axion_wizard.steps.s06_deploy.deploy")
    wait_for_healthy = mocker.patch("axion_wizard.steps.s06_deploy.wait_for_healthy")

    result = runner.invoke(
        app, ["--project-dir", str(project), "set-bot-token", "bot-token-de-ejemplo"]
    )

    assert result.exit_code == 0
    assert "MM_BOT_TOKEN=bot-token-de-ejemplo" in (project / ".env").read_text()
    assert deploy.call_args.kwargs["services"] == ["fastapi"]
    wait_for_healthy.assert_called_once()
    # The token must never appear in the terminal output (§9).
    assert "bot-token-de-ejemplo" not in result.stdout


def test_set_bot_token_explains_the_bot_must_be_in_the_channel(tmp_path: Path, mocker) -> None:
    project = _project_with_compose(tmp_path)
    mocker.patch("axion_wizard.steps.s06_deploy.deploy")
    mocker.patch("axion_wizard.steps.s06_deploy.wait_for_healthy")

    result = runner.invoke(app, ["--project-dir", str(project), "set-bot-token", "bot-abc-123"])

    assert "channel" in result.stdout


def test_set_bot_token_rejects_an_empty_token(tmp_path: Path) -> None:
    project = _project_with_compose(tmp_path)
    result = runner.invoke(app, ["--project-dir", str(project), "set-bot-token", "   "])
    assert result.exit_code == 1


def test_set_bot_token_rejects_a_character_that_breaks_the_env(tmp_path: Path) -> None:
    project = _project_with_compose(tmp_path)
    result = runner.invoke(app, ["--project-dir", str(project), "set-bot-token", "tok$en"])
    assert result.exit_code == 1
    assert "variable expansion" in result.stderr


def test_set_bot_token_dry_run_writes_nothing(tmp_path: Path, mocker) -> None:
    project = _project_with_compose(tmp_path)
    deploy = mocker.patch("axion_wizard.steps.s06_deploy.deploy")
    result = runner.invoke(
        app, ["--project-dir", str(project), "--dry-run", "set-bot-token", "bot-abc-123"]
    )
    assert result.exit_code == 0
    deploy.assert_not_called()
    assert "MM_BOT_TOKEN" not in (project / ".env").read_text()


def test_set_bot_token_fails_cleanly_without_a_project(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--project-dir", str(tmp_path), "set-bot-token", "bot-abc-123"])
    assert result.exit_code == 1
    assert "docker-compose.yml" in result.stderr


def test_set_bot_token_needs_elevation_like_the_other_write_commands() -> None:
    from axion_wizard.cli import ELEVATION_REQUIRED_COMMANDS

    assert "set-bot-token" in ELEVATION_REQUIRED_COMMANDS


# --- reset / install --restart --------------------------------------------------------


def test_reset_command_clears_the_progress(tmp_path: Path) -> None:
    from axion_wizard.utils import state as state_store

    previous = state_store.WizardState()
    previous.mark_complete("deploy", "6 servicios operativos")
    state_store.save_state(tmp_path, previous)

    result = runner.invoke(app, ["--project-dir", str(tmp_path), "reset", "--yes"])

    assert result.exit_code == 0
    assert not state_store.state_path(tmp_path).exists()


def test_reset_says_what_it_will_not_touch(tmp_path: Path) -> None:
    from axion_wizard.utils import state as state_store

    state_store.save_state(tmp_path, state_store.WizardState())
    result = runner.invoke(app, ["--project-dir", str(tmp_path), "reset", "--yes"])

    assert "uninstall --purge" in result.stdout


def test_reset_without_progress_is_not_an_error(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--project-dir", str(tmp_path), "reset", "--yes"])
    assert result.exit_code == 0


def test_reset_does_not_require_a_deployed_stack(tmp_path: Path) -> None:
    """Unlike `up`/`logs`, `reset` has to work precisely when the stack is no
    longer there — that is what it is for."""
    from axion_wizard.utils import state as state_store

    state_store.save_state(tmp_path, state_store.WizardState())
    result = runner.invoke(app, ["--project-dir", str(tmp_path), "reset", "--yes"])
    assert result.exit_code == 0
    assert "docker-compose.yml" not in result.stderr


def test_reset_never_asks_for_elevation() -> None:
    """Deleting a file from the project needs no UAC."""
    from axion_wizard.cli import ELEVATION_REQUIRED_COMMANDS

    assert "reset" not in ELEVATION_REQUIRED_COMMANDS
