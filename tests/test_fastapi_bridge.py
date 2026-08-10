"""El puente FastAPI que va empaquetado como plantilla.

Se prueba de verdad, no solo como texto: el archivo se importa como módulo
con el entorno que tendría dentro del contenedor y se ejerce con el cliente
de pruebas de FastAPI. Antes solo se comprobaba que el archivo se copiara al
proyecto, así que su lógica —validación del token, modo asíncrono, manejo de
errores de Ollama— no la cubría nada.

Lo que hay detrás del modo asíncrono: Mattermost espera la respuesta HTTP
del webhook saliente y la abandona a los ~30s. Un modelo de 7B en CPU tarda
más que eso con facilidad, así que en modo síncrono la respuesta se pierde
entera y desde fuera parece que la IA no contesta, sin nada en los logs que
lo explique.
"""

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

from axion_wizard.utils.resources import template_filesystem_path

# `fastapi` es dependencia de desarrollo, no del wizard: el puente solo corre
# dentro del contenedor. El camino de arranque con pip instala una lista corta
# de herramientas y puede no traerlo, así que se salta en vez de romper la
# suite entera por algo que no es un fallo del código.
TestClient = pytest.importorskip(
    "fastapi.testclient", reason="fastapi no está instalado (grupo dev)"
).TestClient


def _load_bridge(monkeypatch, **env: str):
    """Importa `templates/fastapi/main.py` con un entorno concreto.

    Las constantes del módulo se leen a nivel superior (es como se comporta
    dentro del contenedor), así que el entorno debe estar puesto antes de
    importarlo y el módulo recargarse por cada combinación.
    """
    for key in (
        "OLLAMA_HOST",
        "OLLAMA_MODEL",
        "OLLAMA_SYSTEM_PROMPT",
        "OLLAMA_TIMEOUT_SECONDS",
        "MM_WEBHOOK_TOKEN",
        "MM_BOT_TOKEN",
        "MM_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    with template_filesystem_path("fastapi/main.py") as path:
        spec = importlib.util.spec_from_file_location("axion_bridge_under_test", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["axion_bridge_under_test"] = module
        spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _drop_module_between_tests():
    yield
    sys.modules.pop("axion_bridge_under_test", None)


def _webhook_form(**overrides: str) -> dict[str, str]:
    form = {"text": "hola", "channel_id": "canal123", "post_id": "post456", "token": ""}
    form.update(overrides)
    return form


# --- modo y salud -------------------------------------------------------------------


def test_health_reports_sync_mode_without_a_bot_token(monkeypatch) -> None:
    bridge = _load_bridge(monkeypatch)
    with TestClient(bridge.app) as client:
        assert client.get("/health").json() == {"status": "ok", "mode": "sync"}


def test_health_reports_async_mode_with_a_bot_token(monkeypatch) -> None:
    bridge = _load_bridge(monkeypatch, MM_BOT_TOKEN="bot-token")
    with TestClient(bridge.app) as client:
        assert client.get("/health").json() == {"status": "ok", "mode": "async"}


# --- validación del token del webhook -------------------------------------------------


def test_rejects_a_wrong_token(monkeypatch) -> None:
    bridge = _load_bridge(monkeypatch, MM_WEBHOOK_TOKEN="el-bueno")
    with TestClient(bridge.app) as client:
        response = client.post("/webhook/mattermost", data=_webhook_form(token="el-malo"))
    assert response.status_code == 403


def test_accepts_the_right_token(monkeypatch, mocker) -> None:
    bridge = _load_bridge(monkeypatch, MM_WEBHOOK_TOKEN="el-bueno")
    mocker.patch.object(bridge, "generate", mocker.AsyncMock(return_value="respuesta"))
    with TestClient(bridge.app) as client:
        response = client.post("/webhook/mattermost", data=_webhook_form(token="el-bueno"))
    assert response.status_code == 200
    assert response.json() == {"text": "respuesta"}


def test_without_a_configured_token_nothing_is_validated(monkeypatch, mocker) -> None:
    """Mismo comportamiento que antes de que existiera el campo."""
    bridge = _load_bridge(monkeypatch)
    mocker.patch.object(bridge, "generate", mocker.AsyncMock(return_value="ok"))
    with TestClient(bridge.app) as client:
        response = client.post("/webhook/mattermost", data=_webhook_form(token="cualquiera"))
    assert response.status_code == 200


# --- modo síncrono --------------------------------------------------------------------


def test_sync_mode_returns_the_answer_in_the_response(monkeypatch, mocker) -> None:
    bridge = _load_bridge(monkeypatch)
    mocker.patch.object(bridge, "generate", mocker.AsyncMock(return_value="42"))
    with TestClient(bridge.app) as client:
        response = client.post("/webhook/mattermost", data=_webhook_form())
    assert response.json() == {"text": "42"}


# --- modo asíncrono -------------------------------------------------------------------


def test_async_mode_answers_immediately_with_an_empty_body(monkeypatch, mocker) -> None:
    """Responder ya, con el cuerpo vacío, es lo que quita el techo de 30s:
    Mattermost lo interpreta como "sin respuesta que publicar" y el mensaje
    de verdad llega después por la API."""
    bridge = _load_bridge(monkeypatch, MM_BOT_TOKEN="bot-token")
    mocker.patch.object(bridge, "generate", mocker.AsyncMock(return_value="respuesta larga"))
    posted = mocker.patch.object(bridge, "post_to_channel", mocker.AsyncMock())

    with TestClient(bridge.app) as client:
        response = client.post("/webhook/mattermost", data=_webhook_form())

    assert response.json() == {}
    posted.assert_awaited_once()
    assert posted.await_args.args[0] == "canal123"
    assert posted.await_args.args[1] == "respuesta larga"


def test_async_mode_threads_the_reply_under_the_original_post(monkeypatch, mocker) -> None:
    bridge = _load_bridge(monkeypatch, MM_BOT_TOKEN="bot-token")
    mocker.patch.object(bridge, "generate", mocker.AsyncMock(return_value="r"))
    posted = mocker.patch.object(bridge, "post_to_channel", mocker.AsyncMock())

    with TestClient(bridge.app) as client:
        client.post("/webhook/mattermost", data=_webhook_form(post_id="post456"))

    assert posted.await_args.kwargs["root_id"] == "post456"


def test_async_mode_falls_back_to_sync_without_a_channel_id(monkeypatch, mocker) -> None:
    """Sin `channel_id` no hay dónde publicar; mejor contestar en la propia
    petición que perder la respuesta."""
    bridge = _load_bridge(monkeypatch, MM_BOT_TOKEN="bot-token")
    mocker.patch.object(bridge, "generate", mocker.AsyncMock(return_value="r"))
    with TestClient(bridge.app) as client:
        response = client.post("/webhook/mattermost", data=_webhook_form(channel_id=""))
    assert response.json() == {"text": "r"}


def test_background_answer_does_not_post_an_empty_message(monkeypatch, mocker) -> None:
    bridge = _load_bridge(monkeypatch, MM_BOT_TOKEN="bot-token")
    mocker.patch.object(bridge, "generate", mocker.AsyncMock(return_value="   "))
    posted = mocker.patch.object(bridge, "post_to_channel", mocker.AsyncMock())

    with TestClient(bridge.app) as client:
        client.post("/webhook/mattermost", data=_webhook_form())

    posted.assert_not_awaited()


def test_a_failure_posting_back_never_escapes(monkeypatch, mocker) -> None:
    """La tarea de fondo corre DESPUÉS de haber respondido a Mattermost: una
    excepción ahí no la ve nadie, solo ensucia el log."""
    bridge = _load_bridge(monkeypatch, MM_BOT_TOKEN="bot-token")
    mocker.patch.object(bridge, "generate", mocker.AsyncMock(return_value="r"))
    mocker.patch.object(
        bridge, "post_to_channel", mocker.AsyncMock(side_effect=httpx.ConnectError("caído"))
    )

    with TestClient(bridge.app) as client:
        response = client.post("/webhook/mattermost", data=_webhook_form())

    assert response.status_code == 200


# --- errores de Ollama, en lenguaje del usuario ----------------------------------------


def test_a_timeout_becomes_a_readable_message(monkeypatch, mocker) -> None:
    """Un 500 con traceback sale en Mattermost como un webhook roto y sin
    explicación; esto le dice al usuario qué hacer."""
    bridge = _load_bridge(monkeypatch, OLLAMA_MODEL="llama3.1:70b")
    mocker.patch.object(
        bridge.httpx.AsyncClient,
        "post",
        mocker.AsyncMock(side_effect=httpx.ReadTimeout("tardó")),
    )

    with TestClient(bridge.app) as client:
        response = client.post("/webhook/mattermost", data=_webhook_form())

    assert response.status_code == 200
    assert "llama3.1:70b" in response.json()["text"]
    assert "axion-wizard model" in response.json()["text"]


def test_an_unreachable_ollama_becomes_a_readable_message(monkeypatch, mocker) -> None:
    bridge = _load_bridge(monkeypatch)
    mocker.patch.object(
        bridge.httpx.AsyncClient,
        "post",
        mocker.AsyncMock(side_effect=httpx.ConnectError("rechazado")),
    )

    with TestClient(bridge.app) as client:
        response = client.post("/webhook/mattermost", data=_webhook_form())

    assert response.status_code == 200
    assert "Ollama" in response.json()["text"]


# --- el prompt de sistema llega a Ollama ------------------------------------------------


def test_the_system_prompt_is_sent_to_ollama(monkeypatch, mocker) -> None:
    bridge = _load_bridge(monkeypatch, OLLAMA_SYSTEM_PROMPT="Responde en gallego.")
    post = mocker.patch.object(
        bridge.httpx.AsyncClient,
        "post",
        mocker.AsyncMock(
            return_value=mocker.Mock(
                status_code=200,
                json=lambda: {"response": "ola"},
                raise_for_status=lambda: None,
            )
        ),
    )

    with TestClient(bridge.app) as client:
        client.post("/webhook/mattermost", data=_webhook_form())

    assert post.await_args.kwargs["json"]["system"] == "Responde en gallego."


def test_no_system_key_is_sent_when_there_is_no_prompt(monkeypatch, mocker) -> None:
    bridge = _load_bridge(monkeypatch)
    post = mocker.patch.object(
        bridge.httpx.AsyncClient,
        "post",
        mocker.AsyncMock(
            return_value=mocker.Mock(
                status_code=200,
                json=lambda: {"response": "hola"},
                raise_for_status=lambda: None,
            )
        ),
    )

    with TestClient(bridge.app) as client:
        client.post("/webhook/mattermost", data=_webhook_form())

    assert "system" not in post.await_args.kwargs["json"]


def test_the_bridge_template_is_valid_python() -> None:
    """El archivo va empaquetado y se copia tal cual al proyecto: un error de
    sintaxis solo se vería al construir la imagen, muy tarde."""
    with template_filesystem_path("fastapi/main.py") as path:
        compile(Path(path).read_text(encoding="utf-8"), str(path), "exec")
