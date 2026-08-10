"""A three-tier Ollama model catalogue, and downloads with progress (§5).

1. **Installed**: `GET /api/tags` against the port published on loopback
   (127.0.0.1:11434, see `docker-compose.yml.j2`). Shown first.
2. **Remote catalogue**: Ollama's public library. Its API is neither stable
   nor formally versioned — it is wrapped in try/except with a short timeout
   and degrades silently to tier 3 if it fails for any reason.
3. **Embedded fallback**: a curated list inside the binary, with approximate
   size and recommended RAM/VRAM. A safety net for installs with no internet,
   or for when the remote API changes shape.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from axion_wizard.errors import OllamaError

OLLAMA_LOCAL_BASE_URL = "http://127.0.0.1:11434"
OLLAMA_LOCAL_TIMEOUT = 5.0
OLLAMA_REMOTE_CATALOG_URL = "https://ollama.com/api/tags"
OLLAMA_REMOTE_TIMEOUT = 5.0
OLLAMA_PULL_TIMEOUT = 3600.0

OTHER_MODEL_SENTINEL = "__other__"


@dataclass
class ModelInfo:
    name: str
    size_bytes: int
    min_ram_gb: float
    needs_gpu: bool
    tags: list[str] = field(default_factory=list)
    installed: bool = False
    #: `True` if `min_ram_gb`/`needs_gpu` came from guessing the parameter
    #: count out of the name (`estimate_requirements_from_name`) rather than
    #: from a real figure. It is what lets the curated catalogue correct them:
    #: without this flag, `enrich_from_embedded_catalog` cannot tell an
    #: estimate from a good figure, because both are a number > 0.
    requirements_estimated: bool = False

    @property
    def size_gb(self) -> float:
        return self.size_bytes / (1024**3)


# --- Tier 1: models already installed ----------------------------------------


async def list_installed_models(
    base_url: str = OLLAMA_LOCAL_BASE_URL, timeout: float = OLLAMA_LOCAL_TIMEOUT
) -> list[dict]:
    """Does not raise if Ollama is not running or does not answer in time — it
    returns an empty list, which is valid input for the rest of the flow."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.get(f"{base_url}/api/tags")
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError):
            return []
    models = data.get("models", [])
    return models if isinstance(models, list) else []


def installed_model_names(installed: list[dict]) -> set[str]:
    return {m["name"] for m in installed if isinstance(m, dict) and m.get("name")}


# --- Tier 2: remote catalogue -------------------------------------------------


#: GB of RAM per billion parameters, at ~4-bit quantisation (what Ollama
#: serves by default) plus headroom for the context window and the runtime.
GB_PER_BILLION_PARAMS = 0.75

#: Past this size, running on CPU stops being practical.
GPU_REQUIRED_PARAMS_B = 30.0

#: `qwen2.5:7b`, `llama3.1:70b`, `mistral-large-3:675b`, `nemotron-3-nano:30b`…
_PARAM_COUNT_RE = re.compile(r"[:\-](\d+(?:\.\d+)?)\s*b\b", re.IGNORECASE)


def estimate_requirements_from_name(name: str) -> tuple[float, bool] | None:
    """Infer `(min_ram_gb, needs_gpu)` from the parameter count in the name.

    Ollama's public library returns little more than names: no recommended RAM
    and no indication of whether a GPU is needed. Without this, every remote
    model would arrive with requirements of 0 and be advertised as compatible
    — `mistral-large-3:675b` included — which is the exact opposite of what §5
    asks for. The parameter suffix (`:7b`, `:675b`) is a universal convention
    in those names and estimates the order of magnitude quite well.

    Returns `None` if the name exposes no recognisable count.
    """
    match = _PARAM_COUNT_RE.search(name)
    if not match:
        return None
    params_b = float(match.group(1))
    return params_b * GB_PER_BILLION_PARAMS, params_b >= GPU_REQUIRED_PARAMS_B


def _parse_remote_catalog_entry(entry: dict) -> ModelInfo | None:
    name = entry.get("name")
    if not name:
        return None

    min_ram_gb = float(entry.get("min_ram_gb", 0) or 0)
    needs_gpu = bool(entry.get("needs_gpu", False))
    estimated = False

    # Only estimate when the API supplied nothing: a real value always wins.
    if min_ram_gb <= 0:
        estimate = estimate_requirements_from_name(name)
        if estimate is not None:
            min_ram_gb, estimated_gpu = estimate
            needs_gpu = needs_gpu or estimated_gpu
            estimated = True

    return ModelInfo(
        name=name,
        size_bytes=int(entry.get("size_bytes", 0) or 0),
        min_ram_gb=min_ram_gb,
        needs_gpu=needs_gpu,
        tags=list(entry.get("tags", []) or []),
        requirements_estimated=estimated,
    )


def enrich_from_embedded_catalog(models: list[ModelInfo]) -> list[ModelInfo]:
    """Correct with the curated catalogue any requirement that is not a real
    figure.

    For models we know first-hand, the curated figure beats any estimate from
    the name — and that was precisely the part that did not work: the
    condition was `min_ram_gb <= 0`, but `_parse_remote_catalog_entry` had
    already filled the field with its estimate, so it **never** applied to any
    name carrying a parameter suffix (`:0.5b`, `:7b`…) — which is to say,
    almost none of them. `qwen2.5:0.5b` was advertised at 0.4 GB instead of
    its real 2.0, and since `recommended_model` picks the most demanding model
    that fits, the recommendation came out skewed.

    The order of preference ends up: real figure from the API > curated
    catalogue > estimate from the name > unknown.
    """
    embedded = {m.name: m for m in get_embedded_catalog()}
    for model in models:
        known = embedded.get(model.name)
        if known is None:
            continue
        if model.min_ram_gb <= 0 or model.requirements_estimated:
            model.min_ram_gb = known.min_ram_gb
            model.needs_gpu = known.needs_gpu
            model.requirements_estimated = False
        if model.size_bytes <= 0:
            model.size_bytes = known.size_bytes
        if not model.tags:
            model.tags = list(known.tags)
    return models


async def fetch_remote_catalog(
    url: str = OLLAMA_REMOTE_CATALOG_URL, timeout: float = OLLAMA_REMOTE_TIMEOUT
) -> list[ModelInfo] | None:
    """A best-effort attempt against Ollama's public library. `None` means
    "unavailable, use the embedded fallback" — it never raises."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None

    entries = payload.get("models") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        return None

    parsed = [_parse_remote_catalog_entry(e) for e in entries if isinstance(e, dict)]
    models = [m for m in parsed if m is not None]
    return models or None


# --- Tier 3: embedded fallback ------------------------------------------------

_EMBEDDED_CATALOG_DATA: tuple[dict, ...] = (
    dict(
        name="qwen2.5:0.5b",
        size_bytes=397_000_000,
        min_ram_gb=2.0,
        needs_gpu=False,
        tags=("chat", "multilingual"),
    ),
    dict(
        name="qwen2.5:1.5b",
        size_bytes=986_000_000,
        min_ram_gb=4.0,
        needs_gpu=False,
        tags=("chat", "multilingual", "tools"),
    ),
    dict(
        name="llama3.2:1b",
        size_bytes=1_300_000_000,
        min_ram_gb=4.0,
        needs_gpu=False,
        tags=("chat",),
    ),
    dict(
        name="llama3.2:3b",
        size_bytes=2_000_000_000,
        min_ram_gb=8.0,
        needs_gpu=False,
        tags=("chat", "tools"),
    ),
    dict(
        name="qwen2.5:7b",
        size_bytes=4_700_000_000,
        min_ram_gb=8.0,
        needs_gpu=False,
        tags=("chat", "multilingual", "tools"),
    ),
    dict(
        name="llama3.1:8b",
        size_bytes=4_900_000_000,
        min_ram_gb=8.0,
        needs_gpu=False,
        tags=("chat", "tools"),
    ),
    dict(
        name="mistral:7b",
        size_bytes=4_100_000_000,
        min_ram_gb=8.0,
        needs_gpu=False,
        tags=("chat",),
    ),
    dict(
        name="qwen2.5:14b",
        size_bytes=9_000_000_000,
        min_ram_gb=16.0,
        needs_gpu=True,
        tags=("chat", "tools"),
    ),
    dict(
        name="llama3.1:70b",
        size_bytes=40_000_000_000,
        min_ram_gb=64.0,
        needs_gpu=True,
        tags=("chat", "tools"),
    ),
)


def get_embedded_catalog() -> list[ModelInfo]:
    """Fresh copies — never hand back references to shared mutable state."""
    return [
        ModelInfo(
            name=d["name"],
            size_bytes=d["size_bytes"],
            min_ram_gb=d["min_ram_gb"],
            needs_gpu=d["needs_gpu"],
            tags=list(d["tags"]),
        )
        for d in _EMBEDDED_CATALOG_DATA
    ]


# --- Hardware fit and presentation order --------------------------------------


def is_model_within_hardware(model: ModelInfo, ram_gb: float, has_gpu: bool) -> bool:
    if model.needs_gpu and not has_gpu:
        return False
    if not has_known_requirements(model):
        # With no data we cannot claim it fits; it is still listed, but it
        # does not count as "fits" and cannot come out recommended.
        return False
    return ram_gb >= model.min_ram_gb


def has_known_requirements(model: ModelInfo) -> bool:
    """`False` when we do not know what the model needs.

    This happens with remote entries whose name does not expose a parameter
    count and which are not in the curated catalogue either. Advertising them
    as "compatible" would be asserting something we have not checked.
    """
    return model.min_ram_gb > 0


def suitability_reason(model: ModelInfo, ram_gb: float, has_gpu: bool) -> str | None:
    """Why a model exceeds the hardware, or `None` if it fits. Shown in yellow
    without hiding the model (§5) — the user decides."""
    if model.needs_gpu and not has_gpu:
        return "needs a dedicated GPU"
    if not has_known_requirements(model):
        return "requirements unknown"
    if ram_gb < model.min_ram_gb:
        return f"needs {model.min_ram_gb:g} GB of free RAM"
    return None


def sort_by_hardware_fit(models: list[ModelInfo], ram_gb: float, has_gpu: bool) -> list[ModelInfo]:
    """Order by fit to the detected hardware, not alphabetically (§5): those
    that fit first (most capable that still fits, first), then those that
    exceed it (closest to fitting, first)."""

    def sort_key(model: ModelInfo) -> tuple[int, float]:
        fits = is_model_within_hardware(model, ram_gb, has_gpu)
        return (0, -model.min_ram_gb) if fits else (1, model.min_ram_gb)

    return sorted(models, key=sort_key)


def recommended_model(models: list[ModelInfo], ram_gb: float, has_gpu: bool) -> ModelInfo | None:
    """The most capable model that still fits the detected hardware, or `None`
    if none fits (the whole catalogue exceeds it)."""
    fitting = [m for m in models if is_model_within_hardware(m, ram_gb, has_gpu)]
    if not fitting:
        return None
    return max(fitting, key=lambda m: m.min_ram_gb)


def mark_installed(models: list[ModelInfo], installed_names: set[str]) -> list[ModelInfo]:
    for model in models:
        model.installed = model.name in installed_names
    return models


async def build_catalog(
    ram_gb: float,
    has_gpu: bool,
    base_url: str = OLLAMA_LOCAL_BASE_URL,
    remote_catalog_url: str = OLLAMA_REMOTE_CATALOG_URL,
) -> list[ModelInfo]:
    """Combine the three tiers: the remote catalogue if it answers, otherwise
    the embedded one; mark what is installed; order by hardware fit.

    The remote tier supplies *freshness* (new names) but no hardware
    requirements, so it is enriched from the curated catalogue before
    ordering; otherwise everything would come out with requirements of 0 and
    be advertised as compatible. Curated models the remote tier does not carry
    are added too, so the reference recommendations are not lost.
    """
    models = await fetch_remote_catalog(url=remote_catalog_url)
    if models:
        enrich_from_embedded_catalog(models)
        known_names = {m.name for m in models}
        models += [m for m in get_embedded_catalog() if m.name not in known_names]
    else:
        models = get_embedded_catalog()

    installed = await list_installed_models(base_url=base_url)
    mark_installed(models, installed_model_names(installed))

    return sort_by_hardware_fit(models, ram_gb=ram_gb, has_gpu=has_gpu)


# --- Downloads with progress --------------------------------------------------


@dataclass
class PullProgress:
    status: str
    completed: int = 0
    total: int = 0

    @property
    def fraction(self) -> float | None:
        if self.total <= 0:
            return None
        return min(self.completed / self.total, 1.0)


async def pull_model(
    name: str,
    on_progress: Callable[[PullProgress], None],
    base_url: str = OLLAMA_LOCAL_BASE_URL,
    timeout: float = OLLAMA_PULL_TIMEOUT,
) -> None:
    """Download a model, parsing the line-delimited JSON stream of `/api/pull`
    (`completed`/`total`) to drive a real progress bar (§5)."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client, client.stream(
            "POST", f"{base_url}/api/pull", json={"name": name}
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                _handle_pull_line(line, name, on_progress)
    except httpx.HTTPError as exc:
        raise OllamaError(
            what=f"Could not reach Ollama to download {name}",
            why=str(exc),
            steps=[
                "Check the `ollama` container is running: axion-wizard up ollama",
                "Check connectivity to ollama.com.",
            ],
        ) from exc


def _handle_pull_line(line: str, name: str, on_progress: Callable[[PullProgress], None]) -> None:
    line = line.strip()
    if not line:
        return
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return

    if data.get("error"):
        raise OllamaError(
            what=f"Failed to download model {name}",
            why=str(data["error"]),
            steps=[f"Check the model exists and retry: axion-wizard models pull {name}"],
        )

    on_progress(
        PullProgress(
            status=data.get("status", ""),
            completed=int(data.get("completed", 0) or 0),
            total=int(data.get("total", 0) or 0),
        )
    )
