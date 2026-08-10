from pathlib import Path

import pytest

from axion_wizard.errors import ConfigError, DeploymentError
from axion_wizard.services import compose
from axion_wizard.utils.shell import CommandNotFoundError, CommandResult


def test_parse_json_lines_or_array_handles_array() -> None:
    output = '[{"Service":"postgres"},{"Service":"mattermost"}]'
    entries = compose._parse_json_lines_or_array(output)
    assert [e["Service"] for e in entries] == ["postgres", "mattermost"]


def test_parse_json_lines_or_array_handles_lines() -> None:
    output = '{"Service":"postgres"}\n{"Service":"mattermost"}\n'
    entries = compose._parse_json_lines_or_array(output)
    assert [e["Service"] for e in entries] == ["postgres", "mattermost"]


def test_parse_json_lines_or_array_handles_single_object() -> None:
    entries = compose._parse_json_lines_or_array('{"Service":"postgres"}')
    assert entries == [{"Service": "postgres"}]


def test_parse_json_lines_or_array_skips_malformed_lines() -> None:
    output = '{"Service":"postgres"}\nnot-json\n{"Service":"mattermost"}\n'
    entries = compose._parse_json_lines_or_array(output)
    assert [e["Service"] for e in entries] == ["postgres", "mattermost"]


def test_parse_json_lines_or_array_empty() -> None:
    assert compose._parse_json_lines_or_array("") == []


PS_OUTPUT = (
    '[{"Service":"postgres","Name":"axion-postgres-1","State":"running",'
    '"Health":"healthy","Image":"postgres:15.13-alpine"},'
    '{"Service":"ollama","Name":"axion-ollama-1","State":"running",'
    '"Health":"","Image":"ollama/ollama:0.6.5"},'
    '{"Service":"wireguard","Name":"axion-wireguard-1","State":"exited",'
    '"Health":"","Image":"ghcr.io/wg-easy/wg-easy:14"}]'
)


def test_parse_ps_output_maps_fields() -> None:
    statuses = compose.parse_ps_output(PS_OUTPUT)
    by_service = {s.service: s for s in statuses}

    assert by_service["postgres"].state == "running"
    assert by_service["postgres"].health == "healthy"
    assert by_service["postgres"].is_running is True
    assert by_service["postgres"].is_healthy_or_no_healthcheck is True

    # sin healthcheck propio: Health == "" -> None, se considera "sano" por defecto
    assert by_service["ollama"].health is None
    assert by_service["ollama"].is_healthy_or_no_healthcheck is True

    assert by_service["wireguard"].is_running is False
    assert by_service["wireguard"].image == "ghcr.io/wg-easy/wg-easy:14"


def test_parse_ps_output_extracts_published_ports() -> None:
    output = (
        '[{"Service":"nginx","Name":"axion-nginx-1","State":"running","Health":"",'
        '"Publishers":[{"URL":"0.0.0.0","TargetPort":443,"PublishedPort":443,"Protocol":"tcp"},'
        '{"URL":"0.0.0.0","TargetPort":80,"PublishedPort":80,"Protocol":"tcp"}]}]'
    )
    statuses = compose.parse_ps_output(output)
    assert statuses[0].published_ports == [80, 443]


def test_parse_ps_output_no_publishers_field_defaults_to_empty() -> None:
    output = '[{"Service":"wireguard","Name":"axion-wireguard-1","State":"running","Health":""}]'
    statuses = compose.parse_ps_output(output)
    assert statuses[0].published_ports == []


def test_container_status_unhealthy_is_not_healthy_or_no_healthcheck() -> None:
    status = compose.ContainerStatus(
        service="mattermost", name="mm", state="running", health="unhealthy"
    )
    assert status.is_healthy_or_no_healthcheck is False


def test_ps_returns_empty_list_on_command_failure(mocker) -> None:
    mocker.patch(
        "axion_wizard.services.compose.run",
        return_value=CommandResult(args=[], returncode=1, stdout="", stderr="boom"),
    )
    assert compose.ps(Path("docker-compose.yml")) == []


def test_ps_parses_successful_output(mocker) -> None:
    mocker.patch(
        "axion_wizard.services.compose.run",
        return_value=CommandResult(args=[], returncode=0, stdout=PS_OUTPUT, stderr=""),
    )
    statuses = compose.ps(Path("docker-compose.yml"))
    assert len(statuses) == 3


def test_get_service_status_finds_matching_service(mocker) -> None:
    mocker.patch(
        "axion_wizard.services.compose.run",
        return_value=CommandResult(args=[], returncode=0, stdout=PS_OUTPUT, stderr=""),
    )
    status = compose.get_service_status(Path("docker-compose.yml"), "wireguard")
    assert status is not None
    assert status.image == "ghcr.io/wg-easy/wg-easy:14"


def test_get_service_status_none_when_not_found(mocker) -> None:
    mocker.patch(
        "axion_wizard.services.compose.run",
        return_value=CommandResult(args=[], returncode=0, stdout=PS_OUTPUT, stderr=""),
    )
    assert compose.get_service_status(Path("docker-compose.yml"), "nonexistent") is None


def test_up_without_on_line_uses_run(mocker) -> None:
    run_mock = mocker.patch(
        "axion_wizard.services.compose.run",
        return_value=CommandResult(args=[], returncode=0, stdout="", stderr=""),
    )
    result = compose.up(Path("docker-compose.yml"))
    assert result.ok is True
    args = run_mock.call_args[0][0]
    assert args[:3] == ["docker", "compose", "-f"]
    assert args[4] == "up"
    assert "-d" in args and "--build" in args


def test_up_with_on_line_uses_run_streaming(mocker) -> None:
    streaming_mock = mocker.patch(
        "axion_wizard.services.compose.run_streaming",
        return_value=CommandResult(args=[], returncode=0, stdout="", stderr=""),
    )
    lines: list[str] = []
    compose.up(Path("docker-compose.yml"), on_line=lines.append)
    streaming_mock.assert_called_once()
    streaming_mock.call_args.kwargs["on_line"]("hello")
    assert lines == ["hello"]


def test_up_with_services_restricts_the_command_to_those_names(mocker) -> None:
    run_mock = mocker.patch(
        "axion_wizard.services.compose.run",
        return_value=CommandResult(args=[], returncode=0, stdout="", stderr=""),
    )
    compose.up(Path("docker-compose.yml"), services=["fastapi"])
    args = run_mock.call_args[0][0]
    assert args[-1] == "fastapi"
    assert "--force-recreate" not in args


def test_up_without_services_touches_the_whole_stack(mocker) -> None:
    run_mock = mocker.patch(
        "axion_wizard.services.compose.run",
        return_value=CommandResult(args=[], returncode=0, stdout="", stderr=""),
    )
    compose.up(Path("docker-compose.yml"))
    args = run_mock.call_args[0][0]
    assert args[-1] == "--build"  # ningún nombre de servicio se añadió


def test_down_without_volumes(mocker) -> None:
    run_mock = mocker.patch(
        "axion_wizard.services.compose.run",
        return_value=CommandResult(args=[], returncode=0, stdout="", stderr=""),
    )
    compose.down(Path("docker-compose.yml"))
    args = run_mock.call_args[0][0]
    assert "--volumes" not in args


def test_down_with_volumes_flag(mocker) -> None:
    run_mock = mocker.patch(
        "axion_wizard.services.compose.run",
        return_value=CommandResult(args=[], returncode=0, stdout="", stderr=""),
    )
    compose.down(Path("docker-compose.yml"), volumes=True)
    args = run_mock.call_args[0][0]
    assert "--volumes" in args


def test_logs_returns_stdout(mocker) -> None:
    mocker.patch(
        "axion_wizard.services.compose.run",
        return_value=CommandResult(
            args=[], returncode=0, stdout="log line 1\nlog line 2\n", stderr=""
        ),
    )
    output = compose.logs(Path("docker-compose.yml"), "mattermost", tail=30)
    assert "log line 1" in output


def test_exec_in_service_builds_correct_args(mocker) -> None:
    run_mock = mocker.patch(
        "axion_wizard.services.compose.run",
        return_value=CommandResult(args=[], returncode=0, stdout="{}", stderr=""),
    )
    compose.exec_in_service(Path("docker-compose.yml"), "ollama", ["curl", "-s", "localhost:11434"])
    args = run_mock.call_args[0][0]
    assert args[-3:] == ["ollama", "curl", "-s"] or "curl" in args
    assert "exec" in args
    assert "-T" in args


def test_build_deployment_failure_error_includes_log_tail(mocker) -> None:
    mocker.patch(
        "axion_wizard.services.compose.run",
        return_value=CommandResult(
            args=[], returncode=0, stdout="error: could not connect\n", stderr=""
        ),
    )
    error = compose.build_deployment_failure_error(Path("docker-compose.yml"), "mattermost")
    assert isinstance(error, DeploymentError)
    assert "mattermost" in error.what
    assert "could not connect" in error.why


def test_build_deployment_failure_error_redacts_dsn_password(mocker) -> None:
    """Regresión (§9): el panel de error muestra los últimos renglones del log
    del contenedor, y Mattermost/PostgreSQL registran su DSN completo —con la
    contraseña— al fallar la conexión. Antes se mostraba en claro."""
    secret = "a1b2c3d4e5f6a1b2c3d4"
    fake_log = (
        '{"level":"error","msg":"failed to connect",'
        f'"dsn":"postgres://mattermost:{secret}@postgres:5432/mattermost"}}'
    )
    mocker.patch(
        "axion_wizard.services.compose.run",
        return_value=CommandResult(args=[], returncode=0, stdout=fake_log, stderr=""),
    )
    error = compose.build_deployment_failure_error(Path("docker-compose.yml"), "mattermost")
    assert secret not in error.why
    assert "failed to connect" in error.why


def test_logs_redacts_registered_secret(mocker) -> None:
    from axion_wizard.utils import secrets as sec

    sec.clear_registered_secrets()
    secret = sec.generate_hex_secret(16)
    sec.register_secret(secret)
    try:
        mocker.patch(
            "axion_wizard.services.compose.run",
            return_value=CommandResult(
                args=[], returncode=0, stdout=f"boot failed using {secret}\n", stderr=""
            ),
        )
        output = compose.logs(Path("docker-compose.yml"), "postgres")
        assert secret not in output
    finally:
        sec.clear_registered_secrets()


def test_config_validate_redacts_secrets_in_stderr(mocker) -> None:
    """Compose interpola el .env antes de validar, así que su stderr puede
    citar una línea ya con la contraseña sustituida."""
    secret = "b2c3d4e5f6a1b2c3d4e5"
    mocker.patch(
        "axion_wizard.services.compose.run",
        return_value=CommandResult(
            args=[],
            returncode=1,
            stdout="",
            stderr=f"invalid: postgres://mattermost:{secret}@postgres:5432/db",
        ),
    )
    with pytest.raises(ConfigError) as exc_info:
        compose.config_validate(Path("docker-compose.yml"))
    assert secret not in exc_info.value.why


def test_build_deployment_failure_error_handles_empty_log(mocker) -> None:
    mocker.patch(
        "axion_wizard.services.compose.run",
        return_value=CommandResult(args=[], returncode=0, stdout="", stderr=""),
    )
    error = compose.build_deployment_failure_error(Path("docker-compose.yml"), "ollama")
    assert "no output" in error.why


# --- never_started / describe_service_state ------------------------------------------
#
# Regresión de un incidente real: un `docker-compose.yml` se borró a mitad de
# `docker compose up -d --build`, y postgres/wireguard (sin dependencias)
# arrancaron bien mientras mattermost/ollama/fastapi/nginx quedaron en
# `created` sin arrancar nunca. El panel de error decía solo "sin salida en
# el log", indistinguible de un contenedor que sí corrió y no escribió nada
# — hubo que salir a `docker compose ps` a mano para ver qué pasaba de
# verdad. `describe_service_state` existe para que el propio panel lo diga.


def test_never_started_true_when_created(mocker) -> None:
    status = compose.ContainerStatus(service="mattermost", name="x", state="created", health=None)
    assert status.never_started is True


def test_never_started_false_when_running(mocker) -> None:
    status = compose.ContainerStatus(service="mattermost", name="x", state="running", health=None)
    assert status.never_started is False


def test_describe_service_state_when_never_created() -> None:
    text = compose.describe_service_state(None, "mattermost")
    assert "was never even created" in text
    assert "mattermost" in text


def test_describe_service_state_when_created_but_never_started() -> None:
    status = compose.ContainerStatus(service="mattermost", name="x", state="created", health=None)
    text = compose.describe_service_state(status, "mattermost")
    assert "never started" in text
    assert "depends_on" in text


def test_describe_service_state_when_exited() -> None:
    status = compose.ContainerStatus(service="mattermost", name="x", state="exited", health=None)
    text = compose.describe_service_state(status, "mattermost")
    assert "started and exited" in text
    assert "exited" in text


def test_describe_service_state_when_running_but_unhealthy() -> None:
    status = compose.ContainerStatus(
        service="mattermost", name="x", state="running", health="unhealthy"
    )
    text = compose.describe_service_state(status, "mattermost")
    assert "healthcheck" in text
    assert "unhealthy" in text


def test_describe_service_state_when_running_and_healthy() -> None:
    status = compose.ContainerStatus(
        service="mattermost", name="x", state="running", health="healthy"
    )
    text = compose.describe_service_state(status, "mattermost")
    assert "running and healthy" in text


def test_failure_error_explains_a_service_that_never_started(mocker) -> None:
    """El caso real: el log está vacío porque el contenedor nunca arrancó, no
    porque haya corrido y callado. El mensaje debe decir la diferencia."""
    ps_json = '[{"Service":"mattermost","Name":"x","State":"created","Health":""}]'

    def fake_run(args, **kwargs):
        if "ps" in args:
            return CommandResult(args=args, returncode=0, stdout=ps_json, stderr="")
        return CommandResult(args=args, returncode=0, stdout="", stderr="")

    mocker.patch("axion_wizard.services.compose.run", side_effect=fake_run)

    error = compose.build_deployment_failure_error(Path("docker-compose.yml"), "mattermost")

    assert "never started" in error.why
    assert "no output in the log" in error.why
    # Con el servicio bloqueado, mirar `ps` de todo el stack va antes que
    # revisar el log de un contenedor que nunca corrió.
    assert "ps" in error.steps[0]


def test_failure_error_notes_when_the_service_was_never_created(mocker) -> None:
    mocker.patch(
        "axion_wizard.services.compose.run",
        return_value=CommandResult(args=[], returncode=0, stdout="[]", stderr=""),
    )
    error = compose.build_deployment_failure_error(Path("docker-compose.yml"), "mattermost")
    assert "was never even created" in error.why


def test_logs_reports_when_the_command_itself_fails(mocker) -> None:
    """Antes, un `docker compose logs` que fallara devolvía stdout vacío
    —indistinguible de un contenedor sin salida— y el stderr real, con la
    causa del fallo, se descartaba en silencio."""
    mocker.patch(
        "axion_wizard.services.compose.run",
        return_value=CommandResult(
            args=[], returncode=1, stdout="", stderr="no such service: mattermost"
        ),
    )
    output = compose.logs(Path("docker-compose.yml"), "mattermost")
    assert "no such service" in output
    assert "could not read the log" in output


def test_logs_command_failure_message_redacts_secrets(mocker) -> None:
    secret = "c3d4e5f6a1b2c3d4e5f6"
    mocker.patch(
        "axion_wizard.services.compose.run",
        return_value=CommandResult(
            args=[],
            returncode=1,
            stdout="",
            stderr=f"invalid: postgres://mattermost:{secret}@postgres:5432/db",
        ),
    )
    output = compose.logs(Path("docker-compose.yml"), "mattermost")
    assert secret not in output


def test_config_validate_ok(mocker) -> None:
    mocker.patch(
        "axion_wizard.services.compose.run",
        return_value=CommandResult(args=[], returncode=0, stdout="", stderr=""),
    )
    compose.config_validate(Path("docker-compose.yml"))  # no debe lanzar


def test_config_validate_docker_missing(mocker) -> None:
    mocker.patch(
        "axion_wizard.services.compose.run", side_effect=CommandNotFoundError("docker")
    )
    with pytest.raises(ConfigError, match="Docker"):
        compose.config_validate(Path("docker-compose.yml"))


def test_config_validate_rejects_invalid_compose(mocker) -> None:
    mocker.patch(
        "axion_wizard.services.compose.run",
        return_value=CommandResult(args=[], returncode=1, stdout="", stderr="yaml: bad indent"),
    )
    with pytest.raises(ConfigError, match="rejected"):
        compose.config_validate(Path("docker-compose.yml"))
