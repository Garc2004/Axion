import asyncio

import httpx
import pytest

from axion_wizard.errors import OllamaError
from axion_wizard.services import ollama

# --- ModelInfo -----------------------------------------------------------------


def test_model_info_size_gb() -> None:
    model = ollama.ModelInfo(name="x", size_bytes=2 * 1024**3, min_ram_gb=4, needs_gpu=False)
    assert model.size_gb == 2.0


# --- Nivel 1: instalados ---------------------------------------------------------


def _mock_async_client(mocker, get_return=None, get_side_effect=None):
    mock_response = mocker.Mock()
    mock_response.raise_for_status = mocker.Mock()
    if get_return is not None:
        mock_response.json.return_value = get_return

    mock_client = mocker.AsyncMock()
    if get_side_effect is not None:
        mock_client.get = mocker.AsyncMock(side_effect=get_side_effect)
    else:
        mock_client.get = mocker.AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = mocker.AsyncMock(return_value=False)
    return mock_client


def test_list_installed_models_success(mocker) -> None:
    payload = {"models": [{"name": "qwen2.5:1.5b"}, {"name": "llama3.2:3b"}]}
    mock_client = _mock_async_client(mocker, get_return=payload)
    mocker.patch("axion_wizard.services.ollama.httpx.AsyncClient", return_value=mock_client)

    models = asyncio.run(ollama.list_installed_models())
    assert [m["name"] for m in models] == ["qwen2.5:1.5b", "llama3.2:3b"]


def test_list_installed_models_http_error_returns_empty(mocker) -> None:
    mock_client = _mock_async_client(mocker, get_side_effect=httpx.ConnectError("refused"))
    mocker.patch("axion_wizard.services.ollama.httpx.AsyncClient", return_value=mock_client)

    assert asyncio.run(ollama.list_installed_models()) == []


def test_list_installed_models_non_list_models_field(mocker) -> None:
    mock_client = _mock_async_client(mocker, get_return={"models": "not-a-list"})
    mocker.patch("axion_wizard.services.ollama.httpx.AsyncClient", return_value=mock_client)

    assert asyncio.run(ollama.list_installed_models()) == []


def test_installed_model_names_filters_missing_names() -> None:
    installed = [{"name": "a"}, {"no_name": "b"}, {"name": ""}, "not-a-dict"]
    assert ollama.installed_model_names(installed) == {"a"}


# --- Nivel 2: catálogo remoto -----------------------------------------------------


def test_parse_remote_catalog_entry_ok() -> None:
    entry = {
        "name": "qwen2.5:7b",
        "size_bytes": 4_700_000_000,
        "min_ram_gb": 8,
        "needs_gpu": False,
        "tags": ["chat"],
    }
    model = ollama._parse_remote_catalog_entry(entry)
    assert model is not None
    assert model.name == "qwen2.5:7b"
    assert model.min_ram_gb == 8.0


def test_parse_remote_catalog_entry_missing_name() -> None:
    assert ollama._parse_remote_catalog_entry({"size_bytes": 1}) is None


def test_fetch_remote_catalog_success(mocker) -> None:
    payload = {
        "models": [
            {"name": "a", "size_bytes": 1, "min_ram_gb": 2, "needs_gpu": False, "tags": []},
            {"name": "b", "size_bytes": 2, "min_ram_gb": 4, "needs_gpu": False, "tags": []},
        ]
    }
    mock_client = _mock_async_client(mocker, get_return=payload)
    mocker.patch("axion_wizard.services.ollama.httpx.AsyncClient", return_value=mock_client)

    models = asyncio.run(ollama.fetch_remote_catalog())
    assert models is not None
    assert [m.name for m in models] == ["a", "b"]


def test_fetch_remote_catalog_accepts_bare_list_payload(mocker) -> None:
    payload = [{"name": "a", "size_bytes": 1, "min_ram_gb": 2, "needs_gpu": False, "tags": []}]
    mock_client = _mock_async_client(mocker, get_return=payload)
    mocker.patch("axion_wizard.services.ollama.httpx.AsyncClient", return_value=mock_client)

    models = asyncio.run(ollama.fetch_remote_catalog())
    assert models is not None
    assert models[0].name == "a"


def test_fetch_remote_catalog_http_error_returns_none(mocker) -> None:
    mock_client = _mock_async_client(mocker, get_side_effect=httpx.ConnectTimeout("slow"))
    mocker.patch("axion_wizard.services.ollama.httpx.AsyncClient", return_value=mock_client)

    assert asyncio.run(ollama.fetch_remote_catalog()) is None


def test_fetch_remote_catalog_unexpected_shape_returns_none(mocker) -> None:
    mock_client = _mock_async_client(mocker, get_return={"unexpected": "shape"})
    mocker.patch("axion_wizard.services.ollama.httpx.AsyncClient", return_value=mock_client)

    assert asyncio.run(ollama.fetch_remote_catalog()) is None


def test_fetch_remote_catalog_empty_list_returns_none(mocker) -> None:
    mock_client = _mock_async_client(mocker, get_return={"models": []})
    mocker.patch("axion_wizard.services.ollama.httpx.AsyncClient", return_value=mock_client)

    assert asyncio.run(ollama.fetch_remote_catalog()) is None


# --- Nivel 3: fallback embebido -----------------------------------------------


def test_get_embedded_catalog_is_non_empty() -> None:
    models = ollama.get_embedded_catalog()
    assert len(models) > 0
    assert all(isinstance(m, ollama.ModelInfo) for m in models)


def test_get_embedded_catalog_returns_fresh_copies_each_call() -> None:
    first = ollama.get_embedded_catalog()
    first[0].installed = True
    first[0].tags.append("mutated")

    second = ollama.get_embedded_catalog()
    assert second[0].installed is False
    assert "mutated" not in second[0].tags


# --- Estimación de requisitos desde el nombre --------------------------------------


@pytest.mark.parametrize(
    ("name", "expected_ram", "expected_gpu"),
    [
        ("qwen2.5:7b", 7 * 0.75, False),
        ("llama3.1:70b", 70 * 0.75, True),
        ("mistral-large-3:675b", 675 * 0.75, True),
        ("nemotron-3-nano:30b", 30 * 0.75, True),
        ("qwen2.5:1.5b", 1.5 * 0.75, False),
        ("gemma4-31b", 31 * 0.75, True),
    ],
)
def test_estimate_requirements_from_name(name, expected_ram, expected_gpu) -> None:
    estimate = ollama.estimate_requirements_from_name(name)
    assert estimate is not None
    ram, gpu = estimate
    assert ram == pytest.approx(expected_ram)
    assert gpu is expected_gpu


@pytest.mark.parametrize("name", ["glm-5.1", "kimi-k3", "deepseek-v4-flash:preview"])
def test_estimate_requirements_from_name_unknown(name: str) -> None:
    assert ollama.estimate_requirements_from_name(name) is None


def test_remote_entry_without_metadata_gets_estimated_requirements() -> None:
    """Regresión: la API pública de Ollama devuelve poco más que nombres, así
    que sin estimación todo entraba con requisitos 0 y se anunciaba como
    compatible — `mistral-large-3:675b` incluido."""
    model = ollama._parse_remote_catalog_entry({"name": "mistral-large-3:675b"})
    assert model is not None
    assert model.min_ram_gb > 100
    assert model.needs_gpu is True
    assert ollama.is_model_within_hardware(model, ram_gb=15.9, has_gpu=False) is False


def test_remote_entry_real_metadata_beats_the_estimate() -> None:
    model = ollama._parse_remote_catalog_entry(
        {"name": "qwen2.5:7b", "min_ram_gb": 3, "needs_gpu": False}
    )
    assert model is not None
    assert model.min_ram_gb == 3.0


def test_enrich_from_embedded_catalog_fills_missing_requirements() -> None:
    bare = [ollama.ModelInfo(name="qwen2.5:1.5b", size_bytes=0, min_ram_gb=0, needs_gpu=False)]
    ollama.enrich_from_embedded_catalog(bare)
    known = {m.name: m for m in ollama.get_embedded_catalog()}["qwen2.5:1.5b"]
    assert bare[0].min_ram_gb == known.min_ram_gb
    assert bare[0].size_bytes == known.size_bytes


def test_enrich_from_embedded_catalog_leaves_unknown_models_alone() -> None:
    bare = [ollama.ModelInfo(name="totally-unknown", size_bytes=0, min_ram_gb=0, needs_gpu=False)]
    ollama.enrich_from_embedded_catalog(bare)
    assert bare[0].min_ram_gb == 0


def test_has_known_requirements() -> None:
    known = ollama.ModelInfo(name="a", size_bytes=1, min_ram_gb=8, needs_gpu=False)
    unknown = ollama.ModelInfo(name="b", size_bytes=0, min_ram_gb=0, needs_gpu=False)
    assert ollama.has_known_requirements(known) is True
    assert ollama.has_known_requirements(unknown) is False


def test_model_with_unknown_requirements_is_not_claimed_compatible() -> None:
    unknown = ollama.ModelInfo(name="mystery", size_bytes=0, min_ram_gb=0, needs_gpu=False)
    assert ollama.is_model_within_hardware(unknown, ram_gb=64, has_gpu=True) is False
    assert ollama.suitability_reason(unknown, 64, True) == "requirements unknown"


def test_model_with_unknown_requirements_is_never_recommended() -> None:
    models = [ollama.ModelInfo(name="mystery", size_bytes=0, min_ram_gb=0, needs_gpu=False)]
    assert ollama.recommended_model(models, ram_gb=64, has_gpu=True) is None


def test_build_catalog_merges_remote_names_with_embedded_requirements(mocker) -> None:
    """El remoto aporta frescura; el curado, los requisitos."""
    remote = [
        ollama.ModelInfo(name="qwen2.5:1.5b", size_bytes=0, min_ram_gb=0, needs_gpu=False),
        ollama.ModelInfo(name="brand-new:70b", size_bytes=0, min_ram_gb=0, needs_gpu=False),
    ]
    mocker.patch(
        "axion_wizard.services.ollama.fetch_remote_catalog",
        mocker.AsyncMock(return_value=remote),
    )
    mocker.patch(
        "axion_wizard.services.ollama.list_installed_models", mocker.AsyncMock(return_value=[])
    )

    catalog = asyncio.run(ollama.build_catalog(ram_gb=16, has_gpu=False))
    by_name = {m.name: m for m in catalog}

    # el conocido recupera sus requisitos curados
    assert by_name["qwen2.5:1.5b"].min_ram_gb > 0
    # y los curados que el remoto no traía siguen presentes
    assert "llama3.1:8b" in by_name


def test_build_catalog_keeps_embedded_models_absent_from_remote(mocker) -> None:
    remote = [ollama.ModelInfo(name="only-remote:7b", size_bytes=0, min_ram_gb=0, needs_gpu=False)]
    mocker.patch(
        "axion_wizard.services.ollama.fetch_remote_catalog",
        mocker.AsyncMock(return_value=remote),
    )
    mocker.patch(
        "axion_wizard.services.ollama.list_installed_models", mocker.AsyncMock(return_value=[])
    )

    catalog = asyncio.run(ollama.build_catalog(ram_gb=16, has_gpu=False))
    names = {m.name for m in catalog}
    assert "only-remote:7b" in names
    assert names.issuperset({m.name for m in ollama.get_embedded_catalog()})


# --- Adecuación al hardware -----------------------------------------------------


def test_is_model_within_hardware_ram_only() -> None:
    model = ollama.ModelInfo(name="x", size_bytes=1, min_ram_gb=8, needs_gpu=False)
    assert ollama.is_model_within_hardware(model, ram_gb=16, has_gpu=False) is True
    assert ollama.is_model_within_hardware(model, ram_gb=4, has_gpu=False) is False


def test_is_model_within_hardware_needs_gpu() -> None:
    model = ollama.ModelInfo(name="x", size_bytes=1, min_ram_gb=8, needs_gpu=True)
    assert ollama.is_model_within_hardware(model, ram_gb=64, has_gpu=False) is False
    assert ollama.is_model_within_hardware(model, ram_gb=64, has_gpu=True) is True


def test_suitability_reason_gpu_takes_priority() -> None:
    model = ollama.ModelInfo(name="x", size_bytes=1, min_ram_gb=64, needs_gpu=True)
    reason = ollama.suitability_reason(model, ram_gb=4, has_gpu=False)
    assert reason == "needs a dedicated GPU"


def test_suitability_reason_ram() -> None:
    model = ollama.ModelInfo(name="x", size_bytes=1, min_ram_gb=16, needs_gpu=False)
    reason = ollama.suitability_reason(model, ram_gb=8, has_gpu=False)
    assert reason == "needs 16 GB of free RAM"


def test_suitability_reason_none_when_fits() -> None:
    model = ollama.ModelInfo(name="x", size_bytes=1, min_ram_gb=8, needs_gpu=False)
    assert ollama.suitability_reason(model, ram_gb=16, has_gpu=False) is None


def test_sort_by_hardware_fit_prefers_fitting_models_first() -> None:
    small = ollama.ModelInfo(name="small", size_bytes=1, min_ram_gb=4, needs_gpu=False)
    big_fitting = ollama.ModelInfo(name="big-fitting", size_bytes=1, min_ram_gb=8, needs_gpu=False)
    too_big = ollama.ModelInfo(name="too-big", size_bytes=1, min_ram_gb=64, needs_gpu=False)
    gpu_only = ollama.ModelInfo(name="gpu-only", size_bytes=1, min_ram_gb=16, needs_gpu=True)

    ordered = ollama.sort_by_hardware_fit(
        [too_big, small, gpu_only, big_fitting], ram_gb=16, has_gpu=False
    )
    names = [m.name for m in ordered]
    # los que encajan van primero, el más "capaz" que aún encaje antes que el más chico
    assert names.index("big-fitting") < names.index("small")
    assert names.index("small") < names.index("gpu-only")
    assert names.index("small") < names.index("too-big")


def test_recommended_model_picks_most_capable_that_fits() -> None:
    models = ollama.get_embedded_catalog()
    rec = ollama.recommended_model(models, ram_gb=8, has_gpu=False)
    assert rec is not None
    assert rec.min_ram_gb <= 8
    fitting = [m for m in models if ollama.is_model_within_hardware(m, 8, False)]
    assert all(m.min_ram_gb <= rec.min_ram_gb for m in fitting)


def test_recommended_model_none_when_nothing_fits() -> None:
    models = ollama.get_embedded_catalog()
    assert ollama.recommended_model(models, ram_gb=0.5, has_gpu=False) is None


def test_mark_installed() -> None:
    models = [
        ollama.ModelInfo(name="a", size_bytes=1, min_ram_gb=1, needs_gpu=False),
        ollama.ModelInfo(name="b", size_bytes=1, min_ram_gb=1, needs_gpu=False),
    ]
    ollama.mark_installed(models, {"a"})
    assert models[0].installed is True
    assert models[1].installed is False


# --- build_catalog (orquestación) -------------------------------------------------


def test_build_catalog_includes_remote_models_when_available(mocker) -> None:
    """El remoto se suma al curado, no lo reemplaza: si lo reemplazara, se
    perderían los requisitos de hardware que solo el curado conoce."""
    remote_models = [
        ollama.ModelInfo(name="remote-model", size_bytes=1, min_ram_gb=4, needs_gpu=False)
    ]
    mocker.patch(
        "axion_wizard.services.ollama.fetch_remote_catalog",
        mocker.AsyncMock(return_value=remote_models),
    )
    mocker.patch(
        "axion_wizard.services.ollama.list_installed_models", mocker.AsyncMock(return_value=[])
    )

    catalog = asyncio.run(ollama.build_catalog(ram_gb=16, has_gpu=False))
    assert "remote-model" in {m.name for m in catalog}


def test_build_catalog_falls_back_to_embedded_when_remote_unavailable(mocker) -> None:
    mocker.patch(
        "axion_wizard.services.ollama.fetch_remote_catalog", mocker.AsyncMock(return_value=None)
    )
    mocker.patch(
        "axion_wizard.services.ollama.list_installed_models", mocker.AsyncMock(return_value=[])
    )

    catalog = asyncio.run(ollama.build_catalog(ram_gb=16, has_gpu=False))
    assert len(catalog) == len(ollama.get_embedded_catalog())


def test_build_catalog_marks_installed_models(mocker) -> None:
    mocker.patch(
        "axion_wizard.services.ollama.fetch_remote_catalog", mocker.AsyncMock(return_value=None)
    )
    mocker.patch(
        "axion_wizard.services.ollama.list_installed_models",
        mocker.AsyncMock(return_value=[{"name": "qwen2.5:1.5b"}]),
    )

    catalog = asyncio.run(ollama.build_catalog(ram_gb=16, has_gpu=False))
    by_name = {m.name: m for m in catalog}
    assert by_name["qwen2.5:1.5b"].installed is True


# --- PullProgress ------------------------------------------------------------------


def test_pull_progress_fraction() -> None:
    progress = ollama.PullProgress(status="downloading", completed=50, total=100)
    assert progress.fraction == 0.5


def test_pull_progress_fraction_none_when_no_total() -> None:
    progress = ollama.PullProgress(status="pulling manifest")
    assert progress.fraction is None


def test_pull_progress_fraction_clamped_to_one() -> None:
    progress = ollama.PullProgress(status="downloading", completed=150, total=100)
    assert progress.fraction == 1.0


# --- _handle_pull_line ---------------------------------------------------------------


def test_handle_pull_line_calls_on_progress() -> None:
    received = []
    ollama._handle_pull_line(
        '{"status":"downloading","completed":10,"total":100}', "m", received.append
    )
    assert len(received) == 1
    assert received[0].completed == 10


def test_handle_pull_line_ignores_blank_line() -> None:
    received = []
    ollama._handle_pull_line("   ", "m", received.append)
    assert received == []


def test_handle_pull_line_ignores_malformed_json() -> None:
    received = []
    ollama._handle_pull_line("not-json", "m", received.append)
    assert received == []


def test_handle_pull_line_raises_on_error_field() -> None:
    with pytest.raises(OllamaError, match="modelo-inexistente"):
        ollama._handle_pull_line(
            '{"error":"model not found"}', "modelo-inexistente", lambda _p: None
        )


# --- pull_model (streaming) -----------------------------------------------------------


def _mock_streaming_client(mocker, lines: list[str], raise_on_status: Exception | None = None):
    mock_response = mocker.AsyncMock()
    if raise_on_status:
        mock_response.raise_for_status = mocker.Mock(side_effect=raise_on_status)
    else:
        mock_response.raise_for_status = mocker.Mock()

    async def aiter_lines():
        for line in lines:
            yield line

    mock_response.aiter_lines = aiter_lines
    mock_response.__aenter__ = mocker.AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = mocker.AsyncMock(return_value=False)

    mock_client = mocker.MagicMock()
    mock_client.stream = mocker.Mock(return_value=mock_response)
    mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = mocker.AsyncMock(return_value=False)
    return mock_client


def test_pull_model_reports_progress(mocker) -> None:
    lines = [
        '{"status":"pulling manifest"}',
        '{"status":"downloading","completed":50,"total":100}',
        '{"status":"success"}',
    ]
    mock_client = _mock_streaming_client(mocker, lines)
    mocker.patch("axion_wizard.services.ollama.httpx.AsyncClient", return_value=mock_client)

    received = []
    asyncio.run(ollama.pull_model("qwen2.5:1.5b", received.append))
    assert [p.status for p in received] == ["pulling manifest", "downloading", "success"]


def test_pull_model_raises_ollama_error_on_http_failure(mocker) -> None:
    mock_client = _mock_streaming_client(mocker, [], raise_on_status=httpx.HTTPStatusError(
        "404", request=httpx.Request("POST", "http://x"), response=httpx.Response(404)
    ))
    mocker.patch("axion_wizard.services.ollama.httpx.AsyncClient", return_value=mock_client)

    with pytest.raises(OllamaError):
        asyncio.run(ollama.pull_model("nonexistent", lambda _p: None))


def test_pull_model_raises_ollama_error_on_stream_error_field(mocker) -> None:
    lines = ['{"error":"model not found"}']
    mock_client = _mock_streaming_client(mocker, lines)
    mocker.patch("axion_wizard.services.ollama.httpx.AsyncClient", return_value=mock_client)

    with pytest.raises(OllamaError):
        asyncio.run(ollama.pull_model("nonexistent", lambda _p: None))


# --- procedencia de los requisitos: dato real > curado > estimación ---------------
#
# Regresión: `_parse_remote_catalog_entry` rellenaba `min_ram_gb` con la
# estimación del nombre, y `enrich_from_embedded_catalog` solo rellenaba si
# valía 0 — así que para cualquier nombre con sufijo de parámetros (`:0.5b`,
# `:7b`… es decir, casi todos) el catálogo curado era código muerto.


def test_estimated_requirements_are_marked_as_such() -> None:
    model = ollama._parse_remote_catalog_entry({"name": "qwen2.5:0.5b"})
    assert model is not None
    assert model.requirements_estimated is True


def test_real_metadata_is_not_marked_as_estimated() -> None:
    model = ollama._parse_remote_catalog_entry(
        {"name": "qwen2.5:7b", "min_ram_gb": 3, "needs_gpu": False}
    )
    assert model is not None
    assert model.requirements_estimated is False


def test_curated_catalog_overrides_a_name_based_estimate() -> None:
    """`qwen2.5:0.5b` estimaba 0,4 GB (0,5 × 0,75) contra los 2,0 curados."""
    estimated = ollama._parse_remote_catalog_entry({"name": "qwen2.5:0.5b"})
    assert estimated is not None
    assert estimated.min_ram_gb < 1.0

    ollama.enrich_from_embedded_catalog([estimated])

    curated = {m.name: m for m in ollama.get_embedded_catalog()}["qwen2.5:0.5b"]
    assert estimated.min_ram_gb == curated.min_ram_gb == 2.0
    assert estimated.requirements_estimated is False


def test_curated_catalog_does_not_override_real_api_metadata() -> None:
    """Un dato que la API sí aportó gana a todo lo demás."""
    real = ollama._parse_remote_catalog_entry(
        {"name": "qwen2.5:1.5b", "min_ram_gb": 3.5, "needs_gpu": False}
    )
    assert real is not None
    ollama.enrich_from_embedded_catalog([real])
    assert real.min_ram_gb == 3.5


def test_estimate_still_applies_to_models_outside_the_curated_catalog() -> None:
    unknown = ollama._parse_remote_catalog_entry({"name": "algo-nuevo:70b"})
    assert unknown is not None
    ollama.enrich_from_embedded_catalog([unknown])
    assert unknown.min_ram_gb == pytest.approx(70 * ollama.GB_PER_BILLION_PARAMS)
    assert unknown.requirements_estimated is True
