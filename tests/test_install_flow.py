"""Tests for the complete install flow (§4) and for resuming it."""

from pathlib import Path

import pytest

from axion_wizard.cli import GlobalState
from axion_wizard.domain.config import AccessMode, AxionConfig, WireguardVariant
from axion_wizard.errors import ConfigError, PlatformError
from axion_wizard.steps import orchestrator
from axion_wizard.steps.base import Step, StepResult
from axion_wizard.steps.context import EnvironmentFacts, InstallContext
from axion_wizard.utils import state as state_store


def _state(tmp_path: Path, **kwargs) -> GlobalState:
    return GlobalState(project_dir=tmp_path, **kwargs)


def _config(tmp_path: Path, **overrides) -> AxionConfig:
    defaults = dict(
        access_mode=AccessMode.LAN,
        host="192.168.1.50",
        wireguard_variant=WireguardVariant.PORTS,
        postgres_password="a" * 64,
        wireguard_admin_username="admin",
        wireguard_admin_password="correct-horse-battery-staple",
        ollama_model="qwen2.5:1.5b",
        project_dir=tmp_path,
    )
    defaults.update(overrides)
    return AxionConfig(**defaults)


class _FakeStep(Step):
    """A fake step that records what gets called on it."""

    def __init__(
        self, state, context, name, *, ok=True, raises=None, still_valid=True, revalidate=True
    ):
        super().__init__(state, context)
        self.name = name
        self.title = name
        self.ok = ok
        self.raises = raises
        #: What `verify()` answers when revalidating a resume: it is what
        #: separates "the step still holds" from "this no longer exists".
        self.still_valid = still_valid
        self.revalidate_on_resume = revalidate
        self.calls: list[str] = []

    def run(self) -> StepResult:
        self.calls.append("run")
        if self.raises is not None:
            raise self.raises
        return StepResult(name=self.name, ok=self.ok, message=f"{self.name} done")

    def verify(self) -> StepResult:
        self.calls.append("verify")
        return StepResult(
            name=self.name,
            ok=self.ok and self.still_valid,
            message="" if self.still_valid else "no longer applied",
        )

    def restore(self) -> None:
        self.calls.append("restore")


# --- order and persistence ------------------------------------------------------------


def test_runs_every_step_in_order_and_persists_progress(tmp_path: Path) -> None:
    state = _state(tmp_path)
    context = InstallContext(project_dir=tmp_path)
    steps = [_FakeStep(state, context, name) for name in ("uno", "dos", "tres")]

    assert orchestrator.run_steps(state, context, steps) is True
    assert [s.calls for s in steps] == [["run"], ["run"], ["run"]]

    persisted = state_store.load_state(tmp_path)
    assert [record.name for record in persisted.completed_steps] == ["uno", "dos", "tres"]
    assert all(record.ok for record in persisted.completed_steps)


def test_stops_at_the_first_failing_step(tmp_path: Path) -> None:
    """Carrying on after a failed step would leave the stack half-built without warning."""
    state = _state(tmp_path)
    context = InstallContext(project_dir=tmp_path)
    steps = [
        _FakeStep(state, context, "uno"),
        _FakeStep(state, context, "dos", ok=False),
        _FakeStep(state, context, "tres"),
    ]

    assert orchestrator.run_steps(state, context, steps) is False
    assert steps[2].calls == [], "the step after the failure must not run"
    assert state_store.load_state(tmp_path).is_complete("dos") is False


def test_the_last_step_may_fail_without_aborting(tmp_path: Path) -> None:
    """Step 9 only reports: its table has already printed and nothing follows."""
    state = _state(tmp_path)
    context = InstallContext(project_dir=tmp_path)
    steps = [_FakeStep(state, context, "uno"), _FakeStep(state, context, "verify", ok=False)]

    assert orchestrator.run_steps(state, context, steps) is False
    assert steps[1].calls == ["run"]


def test_an_axion_error_is_recorded_and_reraised(tmp_path: Path) -> None:
    state = _state(tmp_path)
    context = InstallContext(project_dir=tmp_path)
    boom = PlatformError(what="no Docker", why="it is required", steps=["install it"])
    steps = [_FakeStep(state, context, "uno", raises=boom)]

    with pytest.raises(PlatformError):
        orchestrator.run_steps(state, context, steps)

    persisted = state_store.load_state(tmp_path)
    assert persisted.is_complete("uno") is False


# --- resuming --------------------------------------------------------------------------


def test_completed_steps_are_restored_not_rerun(tmp_path: Path) -> None:
    """On resume, an already-done step repopulates the context with
    `restore()` rather than asking again: the persisted state holds none of
    its values."""
    previous = state_store.WizardState()
    previous.mark_complete("uno", "done earlier")
    state_store.save_state(tmp_path, previous)

    state = _state(tmp_path)
    context = InstallContext(project_dir=tmp_path)
    steps = [_FakeStep(state, context, "uno"), _FakeStep(state, context, "dos")]

    assert orchestrator.run_steps(state, context, steps) is True
    # `restore` repopulates the context and `verify` confirms the step still
    # holds; what must not appear is `run`.
    assert steps[0].calls == ["restore", "verify"]
    assert "run" not in steps[0].calls, "it must not run again"
    assert steps[1].calls == ["run"]


# --- resuming without trusting the state file --------------------------------------
#
# Regression from a real case: the state said "deploy: 6 services operational"
# after the user uninstalled Docker. `install` skipped the deployment — "it was
# already done" — and landed on step 9 to fail all seven checks, with no hint
# that the problem was seven steps earlier. The file says what happened last
# time, not what is true now.


def test_a_completed_step_that_is_no_longer_valid_is_redone(tmp_path: Path) -> None:
    previous = state_store.WizardState()
    previous.mark_complete("uno", "done earlier")
    state_store.save_state(tmp_path, previous)

    state = _state(tmp_path)
    context = InstallContext(project_dir=tmp_path)
    steps = [_FakeStep(state, context, "uno", still_valid=False)]

    assert orchestrator.run_steps(state, context, steps) is True
    assert steps[0].calls == ["restore", "verify", "run"]


def test_invalidating_a_step_also_discards_everything_after_it(tmp_path: Path) -> None:
    """Whatever came after was built on top of the fallen step, so it stops
    counting too — otherwise the deployment is redone while a verification made
    against the previous stack is still trusted."""
    previous = state_store.WizardState()
    for name in ("uno", "dos", "tres"):
        previous.mark_complete(name, "done earlier")
    state_store.save_state(tmp_path, previous)

    state = _state(tmp_path)
    context = InstallContext(project_dir=tmp_path)
    steps = [
        _FakeStep(state, context, "uno"),
        _FakeStep(state, context, "dos", still_valid=False),
        _FakeStep(state, context, "tres"),
    ]

    assert orchestrator.run_steps(state, context, steps) is True
    assert "run" not in steps[0].calls, "the step before the fallen one still holds"
    assert steps[1].calls == ["restore", "verify", "run"]
    assert steps[2].calls == ["run"], "the later one is redone, not taken on trust"


def test_steps_that_opt_out_are_not_revalidated(tmp_path: Path) -> None:
    """`WireguardStep` waits 30s for the panel to answer and `VerifyStep` runs
    all nine checks: revalidating them would cost dearly and protect nobody,
    because they are the last two."""
    previous = state_store.WizardState()
    previous.mark_complete("uno", "done earlier")
    state_store.save_state(tmp_path, previous)

    state = _state(tmp_path)
    context = InstallContext(project_dir=tmp_path)
    steps = [_FakeStep(state, context, "uno", still_valid=False, revalidate=False)]

    assert orchestrator.run_steps(state, context, steps) is True
    assert steps[0].calls == ["restore"]


def test_the_real_steps_that_opt_out_are_the_last_two() -> None:
    """An explicit anchor: if anyone turns revalidation off on a step the
    later ones do depend on, the original bug comes back."""
    from axion_wizard.steps.s01_environment import EnvironmentStep
    from axion_wizard.steps.s05_compose import ComposeStep
    from axion_wizard.steps.s06_deploy import DeployStep
    from axion_wizard.steps.s08_wireguard import WireguardStep
    from axion_wizard.steps.s09_verify import VerifyStep

    assert EnvironmentStep.revalidate_on_resume is True
    assert ComposeStep.revalidate_on_resume is True
    assert DeployStep.revalidate_on_resume is True
    assert WireguardStep.revalidate_on_resume is False
    assert VerifyStep.revalidate_on_resume is False


def test_a_step_that_cannot_be_restored_is_marked_for_rerun(tmp_path: Path) -> None:
    """If the artifacts are gone, carrying on with a half-built context would
    blow up later and further from the cause."""
    previous = state_store.WizardState()
    previous.mark_complete("config", "done earlier")
    state_store.save_state(tmp_path, previous)

    state = _state(tmp_path)
    context = InstallContext(project_dir=tmp_path)
    step = _FakeStep(state, context, "config")
    step.restore = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
        ConfigError(what="falta .env", why="borrado", steps=["restaurarlo"])
    )

    with pytest.raises(ConfigError):
        orchestrator.run_steps(state, context, [step])

    assert state_store.load_state(tmp_path).is_complete("config") is False


def test_a_step_that_cannot_be_restored_also_invalidates_the_later_ones(
    tmp_path: Path,
) -> None:
    """The same reason as invalidating via `verify()`: whatever came after was
    built on top of this step. Without this, the next run redid only this one
    and went on trusting the rest."""
    previous = state_store.WizardState()
    for name in ("config", "compose", "deploy"):
        previous.mark_complete(name, "done earlier")
    state_store.save_state(tmp_path, previous)

    state = _state(tmp_path)
    context = InstallContext(project_dir=tmp_path)
    steps = [
        _FakeStep(state, context, "config"),
        _FakeStep(state, context, "compose"),
        _FakeStep(state, context, "deploy"),
    ]
    steps[0].restore = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
        ConfigError(what="falta .env", why="borrado", steps=["restaurarlo"])
    )

    with pytest.raises(ConfigError):
        orchestrator.run_steps(state, context, steps)

    persisted = state_store.load_state(tmp_path)
    assert persisted.is_complete("config") is False
    assert persisted.is_complete("compose") is False
    assert persisted.is_complete("deploy") is False


# --- dry-run ----------------------------------------------------------------------------


def test_dry_run_does_not_write_the_state_file(tmp_path: Path) -> None:
    """Marking as done what was not done would make the next real run skip
    steps that were never applied."""
    state = _state(tmp_path, dry_run=True)
    context = InstallContext(project_dir=tmp_path)
    steps = [_FakeStep(state, context, "uno")]

    orchestrator.run_steps(state, context, steps)

    assert not state_store.state_path(tmp_path).exists()


# --- the real composition of the steps ----------------------------------------------------


def test_build_steps_returns_the_ten_steps_in_spec_order(tmp_path: Path) -> None:
    state = _state(tmp_path)
    context = InstallContext(project_dir=tmp_path)
    names = orchestrator.ordered_step_names(orchestrator.build_steps(state, context))
    assert names == [
        "environment",
        "network",
        "config",
        "certificate",
        "compose",
        "deploy",
        "model",
        "wireguard",
        "bot_setup",
        "verify",
    ]


# --- rebuilding the configuration from artifacts ---------------------------------------------


def test_config_is_rebuilt_from_env_files(tmp_path: Path) -> None:
    """This is what makes resuming possible without asking for passwords again."""
    from axion_wizard.steps.s03_config import load_config_from_artifacts

    (tmp_path / ".env").write_text(
        "POSTGRES_PASSWORD=" + "a" * 64 + "\nOLLAMA_MODEL=qwen2.5:1.5b\n"
        "MM_SITEURL=https://192.168.1.50\n",
        encoding="utf-8",
    )
    (tmp_path / "wg.env").write_text(
        "INIT_HOST=192.168.1.50\nINIT_USERNAME=admin\n"
        "INIT_PASSWORD=correct-horse-battery-staple\n",
        encoding="utf-8",
    )

    config = load_config_from_artifacts(tmp_path)

    assert config.host == "192.168.1.50"
    assert config.ollama_model == "qwen2.5:1.5b"
    assert config.postgres_password.get_secret_value() == "a" * 64
    assert config.access_mode is AccessMode.LAN
    # The panel credentials come back too. Under v14 only the hash was stored,
    # so step 8 had to ask for the password again halfway through the install;
    # now it resumes without asking anything.
    assert config.wireguard_admin_username == "admin"
    assert (
        config.wireguard_admin_password.get_secret_value() == "correct-horse-battery-staple"
    )


def test_rebuilding_config_fails_clearly_when_env_is_missing(tmp_path: Path) -> None:
    from axion_wizard.steps.s03_config import load_config_from_artifacts

    with pytest.raises(ConfigError, match="could not be rebuilt"):
        load_config_from_artifacts(tmp_path)


# --- loading from TOML (--unattended) --------------------------------------------------------


def test_toml_config_reads_a_plaintext_password(tmp_path: Path) -> None:
    from axion_wizard.steps.s03_config import load_config_from_toml

    toml = tmp_path / "axion.toml"
    toml.write_text(
        'access_mode = "lan"\nhost = "192.168.1.50"\n'
        'wireguard_admin_password = "panel-seguro-y-largo"\nollama_model = "qwen2.5:1.5b"\n',
        encoding="utf-8",
    )

    config = load_config_from_toml(toml, tmp_path, WireguardVariant.PORTS)

    assert config.host == "192.168.1.50"
    assert config.wireguard_admin_password.get_secret_value() == "panel-seguro-y-largo"
    # Without `wireguard_admin_username`, the same default the interactive
    # path offers is used.
    assert config.wireguard_admin_username == "admin"
    # With no `postgres_password` in the TOML one is generated: hex, never base64.
    generated = config.postgres_password.get_secret_value()
    assert len(generated) == 64
    assert not set(generated) & set("/+=")


def test_existing_postgres_password_reads_from_env(tmp_path: Path) -> None:
    """A direct unit test of the function the interactive and `--unattended`
    paths share: both have to stay consistent with what Postgres already has
    initialised."""
    from axion_wizard.steps.s03_config import existing_postgres_password

    (tmp_path / ".env").write_text("POSTGRES_PASSWORD=" + "c" * 64 + "\n", encoding="utf-8")
    assert existing_postgres_password(tmp_path) == "c" * 64


def test_existing_postgres_password_none_without_env(tmp_path: Path) -> None:
    from axion_wizard.steps.s03_config import existing_postgres_password

    assert existing_postgres_password(tmp_path) is None


def test_toml_config_reuses_the_existing_postgres_password(tmp_path: Path) -> None:
    """A real regression, hit twice in one night: Postgres only applies
    POSTGRES_PASSWORD when it first initialises its volume. Generating a fresh
    random one on every `install --unattended` left an already-initialised
    Postgres with a password that no longer matched `.env`'s, and Mattermost
    could not authenticate — with no clear error until the container's logs
    were read."""
    from axion_wizard.steps.s03_config import load_config_from_toml

    existing_password = "a" * 64
    (tmp_path / ".env").write_text(
        f"POSTGRES_PASSWORD={existing_password}\nOLLAMA_MODEL=qwen2.5:1.5b\n",
        encoding="utf-8",
    )
    toml = tmp_path / "axion.toml"
    toml.write_text(
        'host = "192.168.1.50"\nwireguard_admin_password = "panel-seguro"\n'
        'ollama_model = "qwen2.5:1.5b"\n',
        encoding="utf-8",
    )

    config = load_config_from_toml(toml, tmp_path, WireguardVariant.PORTS)

    assert config.postgres_password.get_secret_value() == existing_password


def test_toml_config_explicit_password_wins_over_existing_env(tmp_path: Path) -> None:
    """If the TOML does carry `postgres_password`, that is an explicit choice
    by the user and it wins over whatever was in `.env`."""
    from axion_wizard.steps.s03_config import load_config_from_toml

    (tmp_path / ".env").write_text(f"POSTGRES_PASSWORD={'a' * 64}\n", encoding="utf-8")
    explicit_password = "b" * 64
    toml = tmp_path / "axion.toml"
    toml.write_text(
        f'host = "192.168.1.50"\npostgres_password = "{explicit_password}"\n'
        'wireguard_admin_password = "panel-seguro"\nollama_model = "qwen2.5:1.5b"\n',
        encoding="utf-8",
    )

    config = load_config_from_toml(toml, tmp_path, WireguardVariant.PORTS)

    assert config.postgres_password.get_secret_value() == explicit_password


def test_toml_config_rejects_a_password_with_a_forbidden_character(tmp_path: Path) -> None:
    from axion_wizard.steps.s03_config import load_config_from_toml

    toml = tmp_path / "axion.toml"
    toml.write_text(
        'host = "192.168.1.50"\nwireguard_admin_password = "has$a-dollar"\n'
        'ollama_model = "qwen2.5:1.5b"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="forbidden character"):
        load_config_from_toml(toml, tmp_path, WireguardVariant.PORTS)


def test_toml_config_reports_a_missing_file(tmp_path: Path) -> None:
    from axion_wizard.steps.s03_config import load_config_from_toml

    with pytest.raises(ConfigError, match="was not found"):
        load_config_from_toml(tmp_path / "no-existe.toml", tmp_path, WireguardVariant.PORTS)


def test_unattended_without_config_explains_what_is_missing(tmp_path: Path) -> None:
    from axion_wizard.steps.s03_config import ConfigStep

    state = _state(tmp_path, unattended=True)
    context = InstallContext(project_dir=tmp_path)
    context.environment = EnvironmentFacts(
        os_info=None,  # type: ignore[arg-type]
        wsl=None,  # type: ignore[arg-type]
        docker=None,  # type: ignore[arg-type]
        hardware=None,  # type: ignore[arg-type]
        wireguard_variant=WireguardVariant.PORTS.value,
    )

    with pytest.raises(ConfigError, match="--config"):
        ConfigStep(state, context).run()


# --- resumen ---------------------------------------------------------------------------------


def test_summary_masks_every_secret(tmp_path: Path) -> None:
    """§9 admits no exceptions, not even on the screen the user has just filled in."""
    from axion_wizard.render.console import console
    from axion_wizard.steps.s03_config import render_summary

    config = _config(tmp_path)
    with console.capture() as capture:
        console.print(render_summary(config))
    rendered = capture.get()

    assert config.postgres_password.get_secret_value() not in rendered
    assert config.wireguard_admin_password.get_secret_value() not in rendered
    assert "****" in rendered
    assert config.host in rendered


# --- reset: starting from scratch on purpose ------------------------------------------


def test_reset_removes_the_progress_file(tmp_path: Path) -> None:
    previous = state_store.WizardState()
    previous.mark_complete("deploy", "6 services operational")
    state_store.save_state(tmp_path, previous)

    from axion_wizard.commands import run_reset

    run_reset(_state(tmp_path, yes=True))

    assert not state_store.state_path(tmp_path).exists()
    assert state_store.load_state(tmp_path).completed_steps == []


def test_reset_is_harmless_without_previous_progress(tmp_path: Path) -> None:
    from axion_wizard.commands import run_reset

    run_reset(_state(tmp_path, yes=True))  # must not raise


def test_reset_does_not_touch_the_deployment_artifacts(tmp_path: Path) -> None:
    """"Redo the steps" and "delete my data" are different things; the second
    is what `uninstall --purge` is for."""
    state_store.save_state(tmp_path, state_store.WizardState())
    for name in (".env", "wg.env", "docker-compose.yml"):
        (tmp_path / name).write_text("contenido\n", encoding="utf-8")

    from axion_wizard.commands import run_reset

    run_reset(_state(tmp_path, yes=True))

    for name in (".env", "wg.env", "docker-compose.yml"):
        assert (tmp_path / name).exists()


def test_reset_dry_run_keeps_the_progress(tmp_path: Path) -> None:
    state_store.save_state(tmp_path, state_store.WizardState())

    from axion_wizard.commands import run_reset

    run_reset(_state(tmp_path, dry_run=True, yes=True))

    assert state_store.state_path(tmp_path).exists()


def test_install_restart_discards_the_previous_progress(tmp_path: Path, mocker) -> None:
    """`--restart` is `reset` + `install` without asking twice."""
    previous = state_store.WizardState()
    previous.mark_complete("deploy", "6 services operational")
    state_store.save_state(tmp_path, previous)

    install = mocker.patch("axion_wizard.steps.orchestrator.install", return_value=True)

    from axion_wizard.commands import run_install

    run_install(_state(tmp_path), restart=True)

    assert not state_store.state_path(tmp_path).exists()
    install.assert_called_once()


# --- the progress map shown when resuming -----------------------------------------


def _render(renderable) -> str:
    """Render with the shared console: the panel's styles are tokens from the
    AXION theme (`axion.border`…) and a bare `Console` would not resolve
    them."""
    from axion_wizard.render.console import console

    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


def test_resume_overview_marks_done_failed_and_where_it_restarts(tmp_path: Path) -> None:
    """The user has to be able to see at a glance why it starts where it
    starts; before there were only eight grey lines in a row and a jump to
    9."""
    previous = state_store.WizardState()
    previous.mark_complete("uno", "done")
    previous.mark_failed("dos", "blew up")
    state_store.save_state(tmp_path, previous)

    state = _state(tmp_path)
    context = InstallContext(project_dir=tmp_path)
    steps = [
        _FakeStep(state, context, "uno"),
        _FakeStep(state, context, "dos"),
        _FakeStep(state, context, "tres"),
    ]

    text = _render(orchestrator.render_resume_overview(steps, previous))

    assert "1/3" in text and "3/3" in text
    assert "blew up" in text
    assert "starts here" in text
    # and how to get out of it if that is not what was wanted
    assert "axion-wizard reset" in text


def test_resume_overview_is_not_shown_without_previous_progress(tmp_path: Path, mocker) -> None:
    render = mocker.patch("axion_wizard.steps.orchestrator.render_resume_overview")
    mocker.patch("axion_wizard.steps.orchestrator.build_steps", return_value=[])
    mocker.patch("axion_wizard.steps.orchestrator.run_steps", return_value=True)

    orchestrator.install(_state(tmp_path))

    render.assert_not_called()
