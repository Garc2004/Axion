"""Catálogo de modelos de Ollama en tres niveles, y descarga con progreso (§5).

1. **Instalados**: `GET /api/tags` contra el puerto publicado en loopback
   (127.0.0.1:11434, ver `docker-compose.yml.j2`). Se muestran primero.
2. **Catálogo remoto**: la librería pública de Ollama. Su API no es estable
   ni está versionada formalmente — va envuelta en try/except con timeout
   corto y degrada en silencio al nivel 3 si falla por cualquier motivo.
3. **Fallback embebido**: lista curada dentro del binario, con tamaño
   aproximado y RAM/VRAM recomendada. Red de seguridad para instalaciones
   sin internet o si la API remota cambia de forma.
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
    #: `True` si `min_ram_gb`/`needs_gpu` salieron de adivinar el recuento de
    #: parámetros del nombre (`estimate_requirements_from_name`) y no de un
    #: dato real. Es lo que permite que el catálogo curado los corrija:
    #: sin esta marca, `enrich_from_embedded_catalog` no puede distinguir una
    #: estimación de un dato bueno, porque las dos son un número > 0.
    requirements_estimated: bool = False

    @property
    def size_gb(self) -> float:
        return self.size_bytes / (1024**3)


# --- Nivel 1: modelos ya instalados ------------------------------------------


async def list_installed_models(
    base_url: str = OLLAMA_LOCAL_BASE_URL, timeout: float = OLLAMA_LOCAL_TIMEOUT
) -> list[dict]:
    """No lanza si Ollama no está corriendo o no responde a tiempo — devuelve
    lista vacía, que es una entrada válida para el resto del flujo."""
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


# --- Nivel 2: catálogo remoto -------------------------------------------------


#: GB de RAM por cada mil millones de parámetros, con cuantización de ~4 bits
#: (lo que sirve Ollama por defecto) más el margen del contexto y el runtime.
GB_PER_BILLION_PARAMS = 0.75

#: A partir de este tamaño, correr en CPU deja de ser práctico.
GPU_REQUIRED_PARAMS_B = 30.0

#: `qwen2.5:7b`, `llama3.1:70b`, `mistral-large-3:675b`, `nemotron-3-nano:30b`…
_PARAM_COUNT_RE = re.compile(r"[:\-](\d+(?:\.\d+)?)\s*b\b", re.IGNORECASE)


def estimate_requirements_from_name(name: str) -> tuple[float, bool] | None:
    """Deduce `(min_ram_gb, needs_gpu)` del recuento de parámetros del nombre.

    La librería pública de Ollama devuelve poco más que nombres: no trae
    RAM recomendada ni si hace falta GPU. Sin esto, todo modelo remoto
    entraría con requisitos 0 y se anunciaría como compatible —
    `mistral-large-3:675b` incluido—, que es justo lo contrario de lo que
    pide §5. El sufijo de parámetros (`:7b`, `:675b`) es una convención
    universal en esos nombres y estima el orden de magnitud bastante bien.

    Devuelve `None` si el nombre no expone un recuento reconocible.
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

    # Solo estimamos si la API no aportó el dato: un valor real siempre gana.
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
    """Corrige con el catálogo curado los requisitos que no son un dato real.

    Para los modelos que conocemos de primera mano, el dato curado es mejor
    que cualquier estimación a partir del nombre — y esa era justo la parte
    que no funcionaba: la condición era `min_ram_gb <= 0`, pero
    `_parse_remote_catalog_entry` ya había rellenado el campo con la
    estimación, así que **nunca** se aplicaba a ningún nombre con sufijo de
    parámetros (`:0.5b`, `:7b`…) — es decir, a casi ninguno. `qwen2.5:0.5b`
    se anunciaba con 0,4 GB en vez de los 2,0 reales, y como
    `recommended_model` elige el más exigente que quepa, la recomendación
    salía sesgada.

    El orden de preferencia queda: dato real de la API > catálogo curado >
    estimación por el nombre > desconocido.
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
    """Intento best-effort contra la librería pública de Ollama. `None`
    significa "no disponible, usar el fallback embebido" — nunca lanza."""
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


# --- Nivel 3: fallback embebido -----------------------------------------------

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
    """Copias frescas — nunca devolver referencias a un estado mutable compartido."""
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


# --- Adecuación al hardware y orden de presentación ---------------------------


def is_model_within_hardware(model: ModelInfo, ram_gb: float, has_gpu: bool) -> bool:
    if model.needs_gpu and not has_gpu:
        return False
    if not has_known_requirements(model):
        # Sin datos no se puede afirmar que quepa; se lista igual, pero no
        # cuenta como "encaja" ni puede salir recomendado.
        return False
    return ram_gb >= model.min_ram_gb


def has_known_requirements(model: ModelInfo) -> bool:
    """`False` cuando no sabemos qué necesita el modelo.

    Pasa con entradas remotas cuyo nombre no expone el número de parámetros
    y que tampoco están en el catálogo curado. Anunciarlas como
    "compatible" sería afirmar algo que no hemos comprobado.
    """
    return model.min_ram_gb > 0


def suitability_reason(model: ModelInfo, ram_gb: float, has_gpu: bool) -> str | None:
    """Motivo por el que un modelo excede el hardware, o `None` si encaja.
    Se muestra en amarillo sin ocultar el modelo (§5) — el usuario decide."""
    if model.needs_gpu and not has_gpu:
        return "requiere GPU dedicada"
    if not has_known_requirements(model):
        return "requisitos desconocidos"
    if ram_gb < model.min_ram_gb:
        return f"necesita {model.min_ram_gb:g} GB de RAM libre"
    return None


def sort_by_hardware_fit(models: list[ModelInfo], ram_gb: float, has_gpu: bool) -> list[ModelInfo]:
    """Ordena por adecuación al hardware detectado, no alfabéticamente (§5):
    primero los que encajan (el más capaz que aún quepa, primero), luego los
    que exceden (el más cercano a encajar, primero)."""

    def sort_key(model: ModelInfo) -> tuple[int, float]:
        fits = is_model_within_hardware(model, ram_gb, has_gpu)
        return (0, -model.min_ram_gb) if fits else (1, model.min_ram_gb)

    return sorted(models, key=sort_key)


def recommended_model(models: list[ModelInfo], ram_gb: float, has_gpu: bool) -> ModelInfo | None:
    """El modelo más capaz que aún encaja en el hardware detectado, o `None`
    si ninguno encaja (todo el catálogo excede el hardware)."""
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
    """Combina los tres niveles: catálogo remoto si responde, si no el
    embebido; marca instalados; ordena por adecuación al hardware.

    El remoto aporta *frescura* (nombres nuevos) pero no requisitos de
    hardware, así que se enriquece con el catálogo curado antes de ordenar;
    si no, todo saldría con requisitos 0 y se anunciaría como compatible.
    Además se añaden los modelos curados que el remoto no traiga, para no
    perder las recomendaciones de referencia.
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


# --- Descarga con progreso -----------------------------------------------------


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
    """Descarga un modelo parseando el stream JSON-por-línea de `/api/pull`
    (`completed`/`total`) para una barra de progreso real (§5)."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client, client.stream(
            "POST", f"{base_url}/api/pull", json={"name": name}
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                _handle_pull_line(line, name, on_progress)
    except httpx.HTTPError as exc:
        raise OllamaError(
            what=f"No se pudo contactar a Ollama para descargar {name}",
            why=str(exc),
            steps=[
                "Verificar que el contenedor `ollama` esté corriendo: axion-wizard up ollama",
                "Verificar conectividad con ollama.com.",
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
            what=f"Fallo al descargar el modelo {name}",
            why=str(data["error"]),
            steps=[f"Verificar que el modelo exista y reintentar: axion-wizard models pull {name}"],
        )

    on_progress(
        PullProgress(
            status=data.get("status", ""),
            completed=int(data.get("completed", 0) or 0),
            total=int(data.get("total", 0) or 0),
        )
    )
