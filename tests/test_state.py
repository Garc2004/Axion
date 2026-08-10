import json
from pathlib import Path

from axion_wizard.utils import state as st

# --- WizardState -----------------------------------------------------------------


def test_is_complete_false_when_absent() -> None:
    state = st.WizardState()
    assert state.is_complete("s01_environment") is False


def test_mark_complete_then_is_complete_true() -> None:
    state = st.WizardState()
    state.mark_complete("s01_environment", "ok")
    assert state.is_complete("s01_environment") is True


def test_mark_failed_is_not_complete() -> None:
    state = st.WizardState()
    state.mark_failed("s02_network", "CGNAT detectado")
    assert state.is_complete("s02_network") is False
    assert state.completed_steps[0].ok is False
    assert state.completed_steps[0].message == "CGNAT detectado"


def test_mark_complete_overwrites_previous_record_for_same_step() -> None:
    state = st.WizardState()
    state.mark_failed("s02_network", "primer intento falló")
    state.mark_complete("s02_network", "reintento OK")
    assert len(state.completed_steps) == 1
    assert state.completed_steps[0].ok is True
    assert state.completed_steps[0].message == "reintento OK"


def test_reset_from_drops_step_and_everything_after() -> None:
    order = ["s01", "s02", "s03", "s04"]
    state = st.WizardState()
    for name in order:
        state.mark_complete(name)

    state.reset_from("s02", order)

    assert state.is_complete("s01") is True
    assert state.is_complete("s02") is False
    assert state.is_complete("s03") is False
    assert state.is_complete("s04") is False


def test_reset_from_noop_when_step_not_in_order() -> None:
    order = ["s01", "s02"]
    state = st.WizardState()
    state.mark_complete("s01")
    state.mark_complete("s02")

    state.reset_from("s99-unknown", order)

    assert state.is_complete("s01") is True
    assert state.is_complete("s02") is True


# --- state_path / load_state / save_state -----------------------------------------


def test_state_path() -> None:
    assert st.state_path(Path("/proj")) == Path("/proj") / ".axion-wizard-state.json"


def test_load_state_missing_file_returns_empty(tmp_path: Path) -> None:
    state = st.load_state(tmp_path)
    assert state.completed_steps == []
    assert state.schema_version == st.STATE_SCHEMA_VERSION


def test_save_state_then_load_state_roundtrip(tmp_path: Path) -> None:
    original = st.WizardState()
    original.mark_complete("s01_environment", "Linux nativo, Docker Engine")
    original.mark_complete("s02_network", "sin CGNAT")

    st.save_state(tmp_path, original)
    loaded = st.load_state(tmp_path)

    assert loaded.schema_version == original.schema_version
    assert [s.name for s in loaded.completed_steps] == ["s01_environment", "s02_network"]
    assert loaded.is_complete("s01_environment") is True


def test_load_state_corrupted_json_returns_empty(tmp_path: Path) -> None:
    st.state_path(tmp_path).write_text("{not valid json", encoding="utf-8")
    state = st.load_state(tmp_path)
    assert state.completed_steps == []


def test_load_state_wrong_schema_version_returns_empty(tmp_path: Path) -> None:
    st.state_path(tmp_path).write_text(
        json.dumps({"schema_version": 999, "completed_steps": []}), encoding="utf-8"
    )
    state = st.load_state(tmp_path)
    assert state.completed_steps == []


def test_load_state_non_dict_json_returns_empty(tmp_path: Path) -> None:
    st.state_path(tmp_path).write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    state = st.load_state(tmp_path)
    assert state.completed_steps == []


def test_load_state_malformed_step_record_returns_empty(tmp_path: Path) -> None:
    st.state_path(tmp_path).write_text(
        json.dumps(
            {
                "schema_version": st.STATE_SCHEMA_VERSION,
                "completed_steps": [{"unexpected_field": "boom"}],
            }
        ),
        encoding="utf-8",
    )
    state = st.load_state(tmp_path)
    assert state.completed_steps == []


def test_save_state_writes_readable_json(tmp_path: Path) -> None:
    state = st.WizardState()
    state.mark_complete("s01_environment")
    st.save_state(tmp_path, state)

    raw = json.loads(st.state_path(tmp_path).read_text(encoding="utf-8"))
    assert raw["schema_version"] == st.STATE_SCHEMA_VERSION
    assert raw["completed_steps"][0]["name"] == "s01_environment"


def test_save_state_is_atomic_and_leaves_no_temp_files(tmp_path: Path) -> None:
    """Regresión: escribir directamente sobre el archivo definitivo dejaba una
    ventana en la que una interrupción —el escenario mismo para el que existe
    este archivo— lo truncaba y se perdía todo el progreso."""
    state = st.WizardState()
    state.mark_complete("s01_environment")
    st.save_state(tmp_path, state)

    leftovers = [p.name for p in tmp_path.iterdir() if p.name != st.STATE_FILENAME]
    assert leftovers == [], f"quedaron temporales sin limpiar: {leftovers}"


def test_save_state_overwrite_keeps_file_valid(tmp_path: Path) -> None:
    first = st.WizardState()
    first.mark_complete("s01")
    st.save_state(tmp_path, first)

    second = st.WizardState()
    second.mark_complete("s01")
    second.mark_complete("s02")
    st.save_state(tmp_path, second)

    loaded = st.load_state(tmp_path)
    assert [s.name for s in loaded.completed_steps] == ["s01", "s02"]


def test_save_state_creates_missing_project_dir(tmp_path: Path) -> None:
    nested = tmp_path / "does" / "not" / "exist"
    state = st.WizardState()
    state.mark_complete("s01")
    st.save_state(nested, state)
    assert st.load_state(nested).is_complete("s01") is True


def test_load_state_recovers_from_a_truncated_file(tmp_path: Path) -> None:
    """Aunque ahora la escritura es atómica, un archivo corrupto de una
    versión anterior no debe impedir arrancar."""
    state = st.WizardState()
    state.mark_complete("s01")
    st.save_state(tmp_path, state)

    full = st.state_path(tmp_path).read_text(encoding="utf-8")
    st.state_path(tmp_path).write_text(full[: len(full) // 2], encoding="utf-8")

    assert st.load_state(tmp_path).completed_steps == []


# --- next_pending_step -------------------------------------------------------------


def test_next_pending_step_returns_first_incomplete() -> None:
    order = ["s01", "s02", "s03"]
    state = st.WizardState()
    state.mark_complete("s01")
    assert st.next_pending_step(order, state) == "s02"


def test_next_pending_step_none_when_all_complete() -> None:
    order = ["s01", "s02"]
    state = st.WizardState()
    state.mark_complete("s01")
    state.mark_complete("s02")
    assert st.next_pending_step(order, state) is None


def test_next_pending_step_first_when_nothing_complete() -> None:
    order = ["s01", "s02"]
    state = st.WizardState()
    assert st.next_pending_step(order, state) == "s01"


def test_next_pending_step_empty_order_returns_none() -> None:
    assert st.next_pending_step([], st.WizardState()) is None
