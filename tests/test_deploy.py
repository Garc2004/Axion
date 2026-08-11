from pathlib import Path

import pytest

from axion_wizard.errors import DeploymentError
from axion_wizard.services.compose import ContainerStatus
from axion_wizard.steps import s06_deploy as s06
from axion_wizard.utils.shell import CommandResult

# --- parse_progress_line / _match_task ---------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Container axion-postgres-1  Started", ("axion-postgres-1", "Started")),
        (" axion-ollama-1 Pulling", ("axion-ollama-1", "Pulling")),
        ("Container axion-wireguard-1  Healthy", ("axion-wireguard-1", "Healthy")),
        ("this line matches nothing useful", None),
        ("", None),
    ],
)
def test_parse_progress_line(line: str, expected) -> None:
    assert s06.parse_progress_line(line) == expected


def test_match_task_finds_service_by_substring() -> None:
    tasks = {"postgres": 1, "ollama": 2}
    assert s06._match_task(tasks, "axion-postgres-1") == 1
    assert s06._match_task(tasks, "axion-ollama-1") == 2
    assert s06._match_task(tasks, "axion-nginx-1") is None


# --- deploy() ------------------------------------------------------------------


def test_deploy_raises_on_failure_with_log_tail(mocker) -> None:
    mocker.patch(
        "axion_wizard.steps.s06_deploy.compose.up",
        return_value=CommandResult(args=[], returncode=1, stdout="", stderr="boom"),
    )
    mocker.patch("axion_wizard.steps.s06_deploy.compose.get_service_status", return_value=None)

    with pytest.raises(DeploymentError):
        s06.deploy(Path("docker-compose.yml"), services=["postgres", "mattermost"])


def test_deploy_succeeds_without_raising(mocker) -> None:
    mocker.patch(
        "axion_wizard.steps.s06_deploy.compose.up",
        return_value=CommandResult(args=[], returncode=0, stdout="", stderr=""),
    )
    s06.deploy(Path("docker-compose.yml"), services=["postgres"])  # must not raise


def test_deploy_streams_lines_into_progress(mocker) -> None:
    captured_lines: list[str] = []

    def fake_up(compose_path, on_line=None, timeout=900.0):
        for line in ["Container axion-postgres-1  Pulling", "Container axion-postgres-1  Started"]:
            captured_lines.append(line)
            if on_line:
                on_line(line)
        return CommandResult(args=[], returncode=0, stdout="", stderr="")

    mocker.patch("axion_wizard.steps.s06_deploy.compose.up", side_effect=fake_up)
    s06.deploy(Path("docker-compose.yml"), services=["postgres"])
    assert len(captured_lines) == 2


def test_first_not_running_service_returns_first_missing(mocker) -> None:
    mocker.patch(
        "axion_wizard.steps.s06_deploy.compose.ps",
        return_value=[
            ContainerStatus(service="postgres", name="p", state="running", health="healthy"),
            ContainerStatus(service="mattermost", name="m", state="exited", health=None),
        ],
    )
    assert s06._first_not_running_service(Path("x"), ["postgres", "mattermost"]) == "mattermost"


def test_first_not_running_service_none_when_all_running(mocker) -> None:
    mocker.patch(
        "axion_wizard.steps.s06_deploy.compose.ps",
        return_value=[
            ContainerStatus(service="postgres", name="p", state="running", health="healthy")
        ],
    )
    assert s06._first_not_running_service(Path("x"), ["postgres"]) is None


def test_readiness_check_runs_one_docker_ps_per_round(mocker) -> None:
    """Efficiency regression: `_service_is_ready` asked per service, and each
    question launched a full `docker compose ps`. With six services that was
    six Docker invocations per retry, in a loop with backoff running for up to
    five minutes."""
    ps_mock = mocker.patch(
        "axion_wizard.steps.s06_deploy.compose.ps",
        return_value=[
            ContainerStatus(service=name, name=name, state="running", health="healthy")
            for name in ("postgres", "mattermost", "nginx", "fastapi")
        ],
    )

    assert s06._all_services_ready(Path("x"), ["postgres", "mattermost", "nginx", "fastapi"])
    assert ps_mock.call_count == 1


# --- check_ollama_ready ---------------------------------------------------------


def test_check_ollama_ready_true_on_success(mocker) -> None:
    mocker.patch(
        "axion_wizard.steps.s06_deploy.compose.exec_in_service",
        return_value=CommandResult(args=[], returncode=0, stdout="NAME  ID\n", stderr=""),
    )
    assert s06.check_ollama_ready(Path("x")) is True


def test_check_ollama_ready_false_on_failure(mocker) -> None:
    mocker.patch(
        "axion_wizard.steps.s06_deploy.compose.exec_in_service",
        return_value=CommandResult(args=[], returncode=1, stdout="", stderr="connection refused"),
    )
    assert s06.check_ollama_ready(Path("x")) is False


# --- _service_is_ready / _all_services_ready ------------------------------------


def test_service_is_ready_false_when_not_running() -> None:
    assert s06._service_is_ready(Path("x"), "postgres", {}) is False


def test_service_is_ready_uses_ollama_check_for_ollama_service(mocker) -> None:
    statuses = {
        "ollama": ContainerStatus(service="ollama", name="o", state="running", health=None)
    }
    ollama_check = mocker.patch(
        "axion_wizard.steps.s06_deploy.check_ollama_ready", return_value=True
    )
    assert s06._service_is_ready(Path("x"), "ollama", statuses) is True
    ollama_check.assert_called_once()


def test_service_is_ready_uses_healthcheck_for_other_services() -> None:
    statuses = {
        "mattermost": ContainerStatus(
            service="mattermost", name="m", state="running", health="healthy"
        )
    }
    assert s06._service_is_ready(Path("x"), "mattermost", statuses) is True


def test_service_is_ready_false_when_unhealthy() -> None:
    statuses = {
        "mattermost": ContainerStatus(
            service="mattermost", name="m", state="running", health="unhealthy"
        )
    }
    assert s06._service_is_ready(Path("x"), "mattermost", statuses) is False


# --- wait_for_healthy ------------------------------------------------------------


def test_wait_for_healthy_succeeds_once_ready(mocker) -> None:
    call_count = {"n": 0}

    def fake_ready(compose_path, services):
        call_count["n"] += 1
        return call_count["n"] >= 3

    mocker.patch("axion_wizard.steps.s06_deploy._all_services_ready", side_effect=fake_ready)
    s06.wait_for_healthy(
        Path("x"), ["postgres"], timeout=5.0, wait_min=0.01, wait_max=0.05
    )
    assert call_count["n"] >= 3


def test_wait_for_healthy_raises_deployment_error_on_timeout(mocker) -> None:
    mocker.patch("axion_wizard.steps.s06_deploy._all_services_ready", return_value=False)
    mocker.patch("axion_wizard.steps.s06_deploy._first_not_ready_service", return_value="postgres")
    build_error_mock = mocker.patch(
        "axion_wizard.steps.s06_deploy.compose.build_deployment_failure_error",
        return_value=DeploymentError(what="x", why="y", steps=[]),
    )
    with pytest.raises(DeploymentError):
        s06.wait_for_healthy(Path("x"), ["postgres"], timeout=0.05)
    build_error_mock.assert_called_once_with(Path("x"), "postgres")


def test_first_not_ready_service(mocker) -> None:
    mocker.patch("axion_wizard.steps.s06_deploy.compose.ps", return_value=[])
    mocker.patch(
        "axion_wizard.steps.s06_deploy._service_is_ready",
        side_effect=lambda _path, name, _statuses: name != "mattermost",
    )
    assert s06._first_not_ready_service(Path("x"), ["postgres", "mattermost"]) == "mattermost"


def test_first_not_ready_service_none_when_all_ready(mocker) -> None:
    mocker.patch("axion_wizard.steps.s06_deploy.compose.ps", return_value=[])
    mocker.patch("axion_wizard.steps.s06_deploy._service_is_ready", return_value=True)
    assert s06._first_not_ready_service(Path("x"), ["postgres"]) is None


# --- refresh_nginx ---------------------------------------------------------------


def _nginx_status(state: str = "running") -> ContainerStatus:
    return ContainerStatus(service="nginx", name="dist-nginx-1", state=state, health=None)


def test_refresh_nginx_restarts_when_mattermost_was_deployed(mocker) -> None:
    """The real case: recreating Mattermost changes its IP, nginx still holds
    the previous one resolved, and the whole stack returns 502 with all six
    containers healthy."""
    mocker.patch(
        "axion_wizard.steps.s06_deploy.compose.get_service_status",
        return_value=_nginx_status(),
    )
    restart = mocker.patch(
        "axion_wizard.steps.s06_deploy.compose.restart",
        return_value=CommandResult(args=[], returncode=0, stdout="", stderr=""),
    )

    assert s06.refresh_nginx(Path("x"), ["postgres", "mattermost", "nginx"]) is True
    restart.assert_called_once_with(Path("x"), "nginx")


def test_refresh_nginx_skips_when_no_upstream_was_deployed(mocker) -> None:
    """`axion-wizard up ollama` touches no nginx upstream: restarting it would
    be a service interruption in exchange for nothing."""
    restart = mocker.patch("axion_wizard.steps.s06_deploy.compose.restart")

    assert s06.refresh_nginx(Path("x"), ["ollama"]) is False
    restart.assert_not_called()


def test_refresh_nginx_skips_when_nginx_is_not_running(mocker) -> None:
    mocker.patch(
        "axion_wizard.steps.s06_deploy.compose.get_service_status",
        return_value=_nginx_status(state="exited"),
    )
    restart = mocker.patch("axion_wizard.steps.s06_deploy.compose.restart")

    assert s06.refresh_nginx(Path("x"), ["mattermost"]) is False
    restart.assert_not_called()


def test_refresh_nginx_raises_when_restart_fails(mocker) -> None:
    """A failed restart leaves the stack on 502; masking that with a
    "successful" deployment is exactly the failure this function exists to
    prevent."""
    mocker.patch(
        "axion_wizard.steps.s06_deploy.compose.get_service_status",
        return_value=_nginx_status(),
    )
    mocker.patch(
        "axion_wizard.steps.s06_deploy.compose.restart",
        return_value=CommandResult(args=[], returncode=1, stdout="", stderr="boom"),
    )
    mocker.patch(
        "axion_wizard.steps.s06_deploy.compose.build_deployment_failure_error",
        return_value=DeploymentError(what="nginx", why="boom", steps=[]),
    )

    with pytest.raises(DeploymentError):
        s06.refresh_nginx(Path("x"), ["mattermost"])


# --- verify_wg_easy_tag ----------------------------------------------------------


def test_verify_wg_easy_tag_accepts_safe_tag(mocker) -> None:
    mocker.patch(
        "axion_wizard.steps.s06_deploy.compose.get_service_status",
        return_value=ContainerStatus(
            service="wireguard", name="w", state="running", health=None,
            image="ghcr.io/wg-easy/wg-easy:15.3.0",
        ),
    )
    s06.verify_wg_easy_tag(Path("x"))  # must not raise


def test_verify_wg_easy_tag_raises_on_v14(mocker) -> None:
    """A hand-edited compose file can leave the container on v14, which
    ignores the INIT_* variables and exposes a different API. None of that
    produces a visible error."""
    mocker.patch(
        "axion_wizard.steps.s06_deploy.compose.get_service_status",
        return_value=ContainerStatus(
            service="wireguard", name="w", state="running", health=None,
            image="ghcr.io/wg-easy/wg-easy:14",
        ),
    )
    with pytest.raises(DeploymentError, match="v14|unsafe"):
        s06.verify_wg_easy_tag(Path("x"))


def test_verify_wg_easy_tag_rejects_untagged_image(mocker) -> None:
    """With no explicit tag Docker resolves to `latest`, which can stop
    pointing at v15 without warning — the case §6.4 exists to head off."""
    mocker.patch(
        "axion_wizard.steps.s06_deploy.compose.get_service_status",
        return_value=ContainerStatus(
            service="wireguard", name="w", state="running", health=None,
            image="ghcr.io/wg-easy/wg-easy",
        ),
    )
    with pytest.raises(DeploymentError, match="unsafe"):
        s06.verify_wg_easy_tag(Path("x"))


def test_verify_wg_easy_tag_handles_port_qualified_registry(mocker) -> None:
    """The registry port's `:` must not be confused with a tag."""
    mocker.patch(
        "axion_wizard.steps.s06_deploy.compose.get_service_status",
        return_value=ContainerStatus(
            service="wireguard", name="w", state="running", health=None,
            image="localhost:5000/wg-easy:15.3.0",
        ),
    )
    s06.verify_wg_easy_tag(Path("x"))  # must not raise


def test_verify_wg_easy_tag_noop_when_status_missing(mocker) -> None:
    mocker.patch("axion_wizard.steps.s06_deploy.compose.get_service_status", return_value=None)
    s06.verify_wg_easy_tag(Path("x"))  # must not raise


def test_verify_wg_easy_tag_noop_when_image_missing(mocker) -> None:
    mocker.patch(
        "axion_wizard.steps.s06_deploy.compose.get_service_status",
        return_value=ContainerStatus(service="wireguard", name="w", state="running", health=None),
    )
    s06.verify_wg_easy_tag(Path("x"))  # must not raise


# --- the password has to reach the container intact --------------------------------


def test_panel_password_check_passes_when_it_arrives_intact(mocker) -> None:
    mocker.patch(
        "axion_wizard.steps.s06_deploy.compose.exec_in_service",
        return_value=CommandResult(
            args=[], returncode=0, stdout="correct-horse-battery\n", stderr=""
        ),
    )
    s06.verify_panel_password_reached_the_container(
        Path("x"), expected="correct-horse-battery"
    )  # must not raise


def test_panel_password_check_catches_a_mangled_value(mocker) -> None:
    """Compose interpolates the values of `env_file:`. Under v14 this caught a
    mangled bcrypt hash; the failure mode is the same now the password travels
    in the clear: the panel starts healthy and lets nobody in."""
    mocker.patch(
        "axion_wizard.steps.s06_deploy.compose.exec_in_service",
        return_value=CommandResult(args=[], returncode=0, stdout="correct-horse-\n", stderr=""),
    )
    with pytest.raises(DeploymentError) as excinfo:
        s06.verify_panel_password_reached_the_container(
            Path("x"), expected="correct-horse-battery"
        )

    assert "different password" in excinfo.value.what
    assert "rejects every login" in excinfo.value.why


def test_panel_password_check_is_silent_when_it_cannot_ask(mocker) -> None:
    """Without `printenv` or without exec nothing can be asserted; declaring
    the deployment failed would be worse than not checking at all."""
    mocker.patch(
        "axion_wizard.steps.s06_deploy.compose.exec_in_service",
        return_value=CommandResult(args=[], returncode=126, stdout="", stderr="no such file"),
    )
    s06.verify_panel_password_reached_the_container(
        Path("x"), expected="correct-horse-battery"
    )  # must not raise
