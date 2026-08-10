"""Tests del flujo de instalación completo (§4) y de su reanudación."""

from pathlib import Path

import pytest

from axion_wizard.cli import GlobalState
from axion_wizard.config import AccessMode, AxionConfig, WireguardVariant
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
        wireguard_admin_password_hash="$2b$12$" + "b" * 53,
        ollama_model="qwen2.5:1.5b",
        project_dir=tmp_path,
    )
    defaults.update(overrides)
    return AxionConfig(**defaults)


class _FakeStep(Step):
    """Paso de mentira que registra lo que le llaman."""

    def __init__(
        self, state, context, name, *, ok=True, raises=None, still_valid=True, revalidate=True
    ):
        super().__init__(state, context)
        self.name = name
        self.title = name
        self.ok = ok
        self.raises = raises
        #: Qué contesta `verify()` al revalidar una reanudación: es lo que
        #: distingue "el paso sigue aplicado" de "esto ya no existe".
        self.still_valid = still_valid
        self.revalidate_on_resume = revalidate
        self.calls: list[str] = []

    def run(self) -> StepResult:
        self.calls.append("run")
        if self.raises is not None:
            raise self.raises
        return StepResult(name=self.name, ok=self.ok, message=f"{self.name} hecho")

    def verify(self) -> StepResult:
        self.calls.append("verify")
        return StepResult(
            name=self.name,
            ok=self.ok and self.still_valid,
            message="" if self.still_valid else "ya no está aplicado",
        )

    def restore(self) -> None:
        self.calls.append("restore")


# --- orden y persistencia -------------------------------------------------------------


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
    """Seguir tras un paso fallido dejaría el stack a medias sin avisar."""
    state = _state(tmp_path)
    context = InstallContext(project_dir=tmp_path)
    steps = [
        _FakeStep(state, context, "uno"),
        _FakeStep(state, context, "dos", ok=False),
        _FakeStep(state, context, "tres"),
    ]

    assert orchestrator.run_steps(state, context, steps) is False
    assert steps[2].calls == [], "el paso posterior al fallo no debe ejecutarse"
    assert state_store.load_state(tmp_path).is_complete("dos") is False


def test_the_last_step_may_fail_without_aborting(tmp_path: Path) -> None:
    """El paso 9 solo informa: su tabla ya se imprimió y no hay nada después."""
    state = _state(tmp_path)
    context = InstallContext(project_dir=tmp_path)
    steps = [_FakeStep(state, context, "uno"), _FakeStep(state, context, "verify", ok=False)]

    assert orchestrator.run_steps(state, context, steps) is False
    assert steps[1].calls == ["run"]


def test_an_axion_error_is_recorded_and_reraised(tmp_path: Path) -> None:
    state = _state(tmp_path)
    context = InstallContext(project_dir=tmp_path)
    boom = PlatformError(what="sin Docker", why="hace falta", steps=["instalarlo"])
    steps = [_FakeStep(state, context, "uno", raises=boom)]

    with pytest.raises(PlatformError):
        orchestrator.run_steps(state, context, steps)

    persisted = state_store.load_state(tmp_path)
    assert persisted.is_complete("uno") is False


# --- reanudación -----------------------------------------------------------------------


def test_completed_steps_are_restored_not_rerun(tmp_path: Path) -> None:
    """Al reanudar, un paso ya hecho repuebla el contexto con `restore()` en
    vez de volver a preguntar: el estado persistido no guarda sus valores."""
    previous = state_store.WizardState()
    previous.mark_complete("uno", "hecho antes")
    state_store.save_state(tmp_path, previous)

    state = _state(tmp_path)
    context = InstallContext(project_dir=tmp_path)
    steps = [_FakeStep(state, context, "uno"), _FakeStep(state, context, "dos")]

    assert orchestrator.run_steps(state, context, steps) is True
    # `restore` repuebla el contexto y `verify` confirma que el paso sigue
    # aplicado; lo que no puede aparecer es `run`.
    assert steps[0].calls == ["restore", "verify"]
    assert "run" not in steps[0].calls, "no debe re-ejecutarse"
    assert steps[1].calls == ["run"]


# --- reanudación que no se cree el archivo de estado -------------------------------
#
# Regresión de un caso real: el estado decía "deploy: 6 servicios operativos"
# después de que el usuario desinstalara Docker. `install` se saltaba el
# despliegue —"ya se hizo"— y aterrizaba en el paso 9 a fallar las siete
# comprobaciones, sin ninguna pista de que el problema estaba siete pasos
# antes. El archivo cuenta lo que pasó la última vez, no lo que hay ahora.


def test_a_completed_step_that_is_no_longer_valid_is_redone(tmp_path: Path) -> None:
    previous = state_store.WizardState()
    previous.mark_complete("uno", "hecho antes")
    state_store.save_state(tmp_path, previous)

    state = _state(tmp_path)
    context = InstallContext(project_dir=tmp_path)
    steps = [_FakeStep(state, context, "uno", still_valid=False)]

    assert orchestrator.run_steps(state, context, steps) is True
    assert steps[0].calls == ["restore", "verify", "run"]


def test_invalidating_a_step_also_discards_everything_after_it(tmp_path: Path) -> None:
    """Lo que venía detrás se construyó sobre el paso caído, así que deja de
    contar también — si no, se rehace el despliegue y se sigue confiando en
    una verificación que se hizo contra el stack anterior."""
    previous = state_store.WizardState()
    for name in ("uno", "dos", "tres"):
        previous.mark_complete(name, "hecho antes")
    state_store.save_state(tmp_path, previous)

    state = _state(tmp_path)
    context = InstallContext(project_dir=tmp_path)
    steps = [
        _FakeStep(state, context, "uno"),
        _FakeStep(state, context, "dos", still_valid=False),
        _FakeStep(state, context, "tres"),
    ]

    assert orchestrator.run_steps(state, context, steps) is True
    assert "run" not in steps[0].calls, "el paso anterior al caído sigue valiendo"
    assert steps[1].calls == ["restore", "verify", "run"]
    assert steps[2].calls == ["run"], "el posterior se rehace, no se da por bueno"


def test_steps_that_opt_out_are_not_revalidated(tmp_path: Path) -> None:
    """`WireguardStep` espera 30s a que responda el panel y `VerifyStep`
    ejecuta las nueve comprobaciones: revalidarlos costaría caro y no
    protege a nadie, porque son los dos últimos."""
    previous = state_store.WizardState()
    previous.mark_complete("uno", "hecho antes")
    state_store.save_state(tmp_path, previous)

    state = _state(tmp_path)
    context = InstallContext(project_dir=tmp_path)
    steps = [_FakeStep(state, context, "uno", still_valid=False, revalidate=False)]

    assert orchestrator.run_steps(state, context, steps) is True
    assert steps[0].calls == ["restore"]


def test_the_real_steps_that_opt_out_are_the_last_two() -> None:
    """Ancla explícita: si alguien desactiva la revalidación en un paso del
    que sí dependen los siguientes, vuelve el bug original."""
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
    """Si los artefactos ya no están, seguir con un contexto a medias
    reventaría más adelante y más lejos de la causa."""
    previous = state_store.WizardState()
    previous.mark_complete("config", "hecho antes")
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
    """Mismo motivo que al invalidar por `verify()`: lo que venía detrás se
    construyó sobre este paso. Sin esto, la siguiente ejecución rehacía solo
    este y volvía a dar por buenos los demás."""
    previous = state_store.WizardState()
    for name in ("config", "compose", "deploy"):
        previous.mark_complete(name, "hecho antes")
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
    """Marcar como hecho lo que no se hizo haría que la ejecución real
    siguiente se saltara pasos que nunca llegaron a aplicarse."""
    state = _state(tmp_path, dry_run=True)
    context = InstallContext(project_dir=tmp_path)
    steps = [_FakeStep(state, context, "uno")]

    orchestrator.run_steps(state, context, steps)

    assert not state_store.state_path(tmp_path).exists()


# --- composición real de los pasos --------------------------------------------------------


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


# --- reconstrucción de la configuración desde artefactos -------------------------------------


def test_config_is_rebuilt_from_env_files(tmp_path: Path) -> None:
    """Es lo que hace posible reanudar sin volver a preguntar contraseñas."""
    from axion_wizard.steps.s03_config import load_config_from_artifacts

    (tmp_path / ".env").write_text(
        "POSTGRES_PASSWORD=" + "a" * 64 + "\nOLLAMA_MODEL=qwen2.5:1.5b\n"
        "MM_SITEURL=https://192.168.1.50\n",
        encoding="utf-8",
    )
    (tmp_path / "wg.env").write_text(
        "WG_HOST=192.168.1.50\nPASSWORD_HASH=$2b$12$" + "b" * 53 + "\n", encoding="utf-8"
    )

    config = load_config_from_artifacts(tmp_path)

    assert config.host == "192.168.1.50"
    assert config.ollama_model == "qwen2.5:1.5b"
    assert config.postgres_password.get_secret_value() == "a" * 64
    assert config.access_mode is AccessMode.LAN


def test_rebuilding_config_fails_clearly_when_env_is_missing(tmp_path: Path) -> None:
    from axion_wizard.steps.s03_config import load_config_from_artifacts

    with pytest.raises(ConfigError, match="reconstruir"):
        load_config_from_artifacts(tmp_path)


# --- carga desde TOML (--unattended) ---------------------------------------------------------


def test_toml_config_hashes_a_plaintext_password(tmp_path: Path) -> None:
    from axion_wizard.steps.s03_config import load_config_from_toml
    from axion_wizard.utils.secrets import verify_password

    toml = tmp_path / "axion.toml"
    toml.write_text(
        'access_mode = "lan"\nhost = "192.168.1.50"\n'
        'wireguard_admin_password = "panel-seguro"\nollama_model = "qwen2.5:1.5b"\n',
        encoding="utf-8",
    )

    config = load_config_from_toml(toml, tmp_path, WireguardVariant.PORTS)

    assert config.host == "192.168.1.50"
    assert verify_password(
        "panel-seguro", config.wireguard_admin_password_hash.get_secret_value()
    )
    # Sin `postgres_password` en el TOML se genera una: hex, nunca base64.
    generated = config.postgres_password.get_secret_value()
    assert len(generated) == 64
    assert not set(generated) & set("/+=")


def testexisting_postgres_password_reads_from_env(tmp_path: Path) -> None:
    """Unidad directa de la función que comparten el camino interactivo y
    el `--unattended`: ambos deben quedar coherentes con lo que Postgres ya
    tiene inicializado."""
    from axion_wizard.steps.s03_config import existing_postgres_password

    (tmp_path / ".env").write_text("POSTGRES_PASSWORD=" + "c" * 64 + "\n", encoding="utf-8")
    assert existing_postgres_password(tmp_path) == "c" * 64


def testexisting_postgres_password_none_without_env(tmp_path: Path) -> None:
    from axion_wizard.steps.s03_config import existing_postgres_password

    assert existing_postgres_password(tmp_path) is None


def test_toml_config_reuses_theexisting_postgres_password(tmp_path: Path) -> None:
    """Regresión real, repetida dos veces la misma noche: Postgres solo
    aplica POSTGRES_PASSWORD al inicializar su volumen la primera vez.
    Generar una nueva al azar en cada `install --unattended` dejaba al
    Postgres ya inicializado con una contraseña que ya no coincidía con la
    de `.env`, y Mattermost no lograba autenticarse — sin ningún error
    claro hasta revisar los logs del contenedor."""
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
    """Si el TOML sí trae `postgres_password`, es una elección explícita del
    usuario y gana sobre lo que hubiera en `.env`."""
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
        'host = "192.168.1.50"\nwireguard_admin_password = "tiene$dolar"\n'
        'ollama_model = "qwen2.5:1.5b"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="prohibido"):
        load_config_from_toml(toml, tmp_path, WireguardVariant.PORTS)


def test_toml_config_reports_a_missing_file(tmp_path: Path) -> None:
    from axion_wizard.steps.s03_config import load_config_from_toml

    with pytest.raises(ConfigError, match="No se encontró"):
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
    """§9 no admite excepciones ni en la pantalla que el usuario acaba de rellenar."""
    from axion_wizard.console import console
    from axion_wizard.steps.s03_config import render_summary

    config = _config(tmp_path)
    with console.capture() as capture:
        console.print(render_summary(config))
    rendered = capture.get()

    assert config.postgres_password.get_secret_value() not in rendered
    assert config.wireguard_admin_password_hash.get_secret_value() not in rendered
    assert "****" in rendered
    assert config.host in rendered


# --- reset: empezar de cero a propósito ----------------------------------------------


def test_reset_removes_the_progress_file(tmp_path: Path) -> None:
    previous = state_store.WizardState()
    previous.mark_complete("deploy", "6 servicios operativos")
    state_store.save_state(tmp_path, previous)

    from axion_wizard.steps.runner import run_reset

    run_reset(_state(tmp_path, yes=True))

    assert not state_store.state_path(tmp_path).exists()
    assert state_store.load_state(tmp_path).completed_steps == []


def test_reset_is_harmless_without_previous_progress(tmp_path: Path) -> None:
    from axion_wizard.steps.runner import run_reset

    run_reset(_state(tmp_path, yes=True))  # no debe lanzar


def test_reset_does_not_touch_the_deployment_artifacts(tmp_path: Path) -> None:
    """"Rehacer los pasos" y "borrar mis datos" son cosas distintas; para la
    segunda está `uninstall --purge`."""
    state_store.save_state(tmp_path, state_store.WizardState())
    for name in (".env", "wg.env", "docker-compose.yml"):
        (tmp_path / name).write_text("contenido\n", encoding="utf-8")

    from axion_wizard.steps.runner import run_reset

    run_reset(_state(tmp_path, yes=True))

    for name in (".env", "wg.env", "docker-compose.yml"):
        assert (tmp_path / name).exists()


def test_reset_dry_run_keeps_the_progress(tmp_path: Path) -> None:
    state_store.save_state(tmp_path, state_store.WizardState())

    from axion_wizard.steps.runner import run_reset

    run_reset(_state(tmp_path, dry_run=True, yes=True))

    assert state_store.state_path(tmp_path).exists()


def test_install_restart_discards_the_previous_progress(tmp_path: Path, mocker) -> None:
    """`--restart` es `reset` + `install` sin preguntar dos veces."""
    previous = state_store.WizardState()
    previous.mark_complete("deploy", "6 servicios operativos")
    state_store.save_state(tmp_path, previous)

    install = mocker.patch("axion_wizard.steps.orchestrator.install", return_value=True)

    from axion_wizard.steps.runner import run_install

    run_install(_state(tmp_path), restart=True)

    assert not state_store.state_path(tmp_path).exists()
    install.assert_called_once()


# --- el mapa de progreso que se enseña al reanudar --------------------------------


def _render(renderable) -> str:
    """Renderiza con la consola compartida: los estilos del panel son tokens
    del tema de AXION (`axion.border`…) y una `Console` pelada no sabría
    resolverlos."""
    from axion_wizard.console import console

    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


def test_resume_overview_marks_done_failed_and_where_it_restarts(tmp_path: Path) -> None:
    """El usuario tiene que poder ver de un vistazo por qué empieza donde
    empieza; antes solo había ocho líneas grises seguidas y un salto al 9."""
    previous = state_store.WizardState()
    previous.mark_complete("uno", "hecho")
    previous.mark_failed("dos", "reventó")
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
    assert "reventó" in text
    assert "empieza aquí" in text
    # y cómo salir de ahí si no es lo que se quería
    assert "axion-wizard reset" in text


def test_resume_overview_is_not_shown_without_previous_progress(tmp_path: Path, mocker) -> None:
    render = mocker.patch("axion_wizard.steps.orchestrator.render_resume_overview")
    mocker.patch("axion_wizard.steps.orchestrator.build_steps", return_value=[])
    mocker.patch("axion_wizard.steps.orchestrator.run_steps", return_value=True)

    orchestrator.install(_state(tmp_path))

    render.assert_not_called()
