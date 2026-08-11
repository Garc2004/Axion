"""The FastAPI bridge that ships as a template.

It is genuinely exercised, not merely checked as text: the file is imported as
a module with the environment it would have inside the container and driven
with FastAPI's test client. Previously only the file being copied into the
project was checked, so its logic — token validation, asynchronous mode,
handling Ollama's errors — was covered by nothing.

What lies behind the asynchronous mode: Mattermost waits for the outgoing
webhook's HTTP response and abandons it after ~30s. A 7B model on CPU easily
takes longer, so in synchronous mode the answer is lost whole and from the
outside it looks as though the AI does not reply, with nothing in the logs to
explain it.
"""

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

from axion_wizard.utils.resources import template_filesystem_path

# `fastapi` is a development dependency, not the wizard's: the bridge only
# runs inside the container. The pip bootstrap path installs a short list of
# tools and may not bring it, so this is skipped rather than breaking the whole
# suite over something that is not a code failure.
TestClient = pytest.importorskip(
    "fastapi.testclient", reason="fastapi is not installed (dev group)"
).TestClient


def _load_bridge(monkeypatch, **env: str):
    """Import `templates/fastapi/main.py` with a specific environment.

    The module's constants are read at import time (which is how it behaves
    inside the container), so the environment has to be set before importing it
    and the module reloaded for each combination.
    """
    for key in (
        "OLLAMA_HOST",
        "OLLAMA_MODEL",
        "OLLAMA_SYSTEM_PROMPT",
        "OLLAMA_TIMEOUT_SECONDS",
        "MM_WEBHOOK_TOKEN",
        "MM_BOT_TOKEN",
        "MM_URL",
        "AI_REPLY_IN_THREAD",
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


# --- mode and health ----------------------------------------------------------------


def test_health_reports_sync_mode_without_a_bot_token(monkeypatch) -> None:
    bridge = _load_bridge(monkeypatch)
    with TestClient(bridge.app) as client:
        assert client.get("/health").json() == {"status": "ok", "mode": "sync"}


def test_health_reports_async_mode_with_a_bot_token(monkeypatch) -> None:
    bridge = _load_bridge(monkeypatch, MM_BOT_TOKEN="bot-token")
    with TestClient(bridge.app) as client:
        assert client.get("/health").json() == {"status": "ok", "mode": "async"}


# --- webhook token validation ---------------------------------------------------------


def test_rejects_a_wrong_token(monkeypatch) -> None:
    bridge = _load_bridge(monkeypatch, MM_WEBHOOK_TOKEN="the-good-one")
    with TestClient(bridge.app) as client:
        response = client.post("/webhook/mattermost", data=_webhook_form(token="the-bad-one"))
    assert response.status_code == 403


def test_accepts_the_right_token(monkeypatch, mocker) -> None:
    bridge = _load_bridge(monkeypatch, MM_WEBHOOK_TOKEN="the-good-one")
    mocker.patch.object(bridge, "generate", mocker.AsyncMock(return_value="respuesta"))
    with TestClient(bridge.app) as client:
        response = client.post("/webhook/mattermost", data=_webhook_form(token="the-good-one"))
    assert response.status_code == 200
    assert response.json() == {"text": "respuesta"}


def test_without_a_configured_token_nothing_is_validated(monkeypatch, mocker) -> None:
    """The same behaviour as before the field existed."""
    bridge = _load_bridge(monkeypatch)
    mocker.patch.object(bridge, "generate", mocker.AsyncMock(return_value="ok"))
    with TestClient(bridge.app) as client:
        response = client.post("/webhook/mattermost", data=_webhook_form(token="cualquiera"))
    assert response.status_code == 200


# --- synchronous mode -----------------------------------------------------------------


def test_sync_mode_returns_the_answer_in_the_response(monkeypatch, mocker) -> None:
    bridge = _load_bridge(monkeypatch)
    mocker.patch.object(bridge, "generate", mocker.AsyncMock(return_value="42"))
    with TestClient(bridge.app) as client:
        response = client.post("/webhook/mattermost", data=_webhook_form())
    assert response.json() == {"text": "42"}


# --- asynchronous mode ----------------------------------------------------------------


def test_async_mode_answers_immediately_with_an_empty_body(monkeypatch, mocker) -> None:
    """Answering immediately, with an empty body, is what removes the 30s
    ceiling: Mattermost reads it as "no reply to post" and the real message
    arrives afterwards through the API."""
    bridge = _load_bridge(monkeypatch, MM_BOT_TOKEN="bot-token")
    mocker.patch.object(bridge, "generate", mocker.AsyncMock(return_value="respuesta larga"))
    posted = mocker.patch.object(bridge, "post_to_channel", mocker.AsyncMock())

    with TestClient(bridge.app) as client:
        response = client.post("/webhook/mattermost", data=_webhook_form())

    assert response.json() == {}
    posted.assert_awaited_once()
    assert posted.await_args.args[0] == "canal123"
    assert posted.await_args.args[1] == "respuesta larga"


def test_async_mode_threads_the_reply_under_the_original_post_by_default(
    monkeypatch, mocker
) -> None:
    bridge = _load_bridge(monkeypatch, MM_BOT_TOKEN="bot-token")
    mocker.patch.object(bridge, "generate", mocker.AsyncMock(return_value="r"))
    posted = mocker.patch.object(bridge, "post_to_channel", mocker.AsyncMock())

    with TestClient(bridge.app) as client:
        client.post("/webhook/mattermost", data=_webhook_form(post_id="post456"))

    assert posted.await_args.kwargs["root_id"] == "post456"


@pytest.mark.parametrize("falsy_value", ["false", "False", "0", "no", "NO"])
def test_async_mode_posts_as_a_plain_message_when_threading_is_disabled(
    monkeypatch, mocker, falsy_value: str
) -> None:
    """The wizard's choice (step 9), not the code's: anyone who prefers to see
    the reply directly in the channel, without opening a thread, can turn it
    off."""
    bridge = _load_bridge(monkeypatch, MM_BOT_TOKEN="bot-token", AI_REPLY_IN_THREAD=falsy_value)
    mocker.patch.object(bridge, "generate", mocker.AsyncMock(return_value="r"))
    posted = mocker.patch.object(bridge, "post_to_channel", mocker.AsyncMock())

    with TestClient(bridge.app) as client:
        client.post("/webhook/mattermost", data=_webhook_form(post_id="post456"))

    assert posted.await_args.kwargs["root_id"] == ""


def test_async_mode_falls_back_to_sync_without_a_channel_id(monkeypatch, mocker) -> None:
    """Without a `channel_id` there is nowhere to post; better to answer
    within the request itself than to lose the reply."""
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
    """The background task runs AFTER Mattermost has been answered: an
    exception there is seen by nobody, it only dirties the log."""
    bridge = _load_bridge(monkeypatch, MM_BOT_TOKEN="bot-token")
    mocker.patch.object(bridge, "generate", mocker.AsyncMock(return_value="r"))
    mocker.patch.object(
        bridge, "post_to_channel", mocker.AsyncMock(side_effect=httpx.ConnectError("down"))
    )

    with TestClient(bridge.app) as client:
        response = client.post("/webhook/mattermost", data=_webhook_form())

    assert response.status_code == 200


# --- Ollama errors, in the user's language --------------------------------------------


def test_a_timeout_becomes_a_readable_message(monkeypatch, mocker) -> None:
    """A 500 with a traceback surfaces in Mattermost as a broken webhook with
    no explanation; this tells the user what to do."""
    bridge = _load_bridge(monkeypatch, OLLAMA_MODEL="llama3.1:70b")
    mocker.patch.object(
        bridge.httpx.AsyncClient,
        "post",
        mocker.AsyncMock(side_effect=httpx.ReadTimeout("took too long")),
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


# --- the system prompt reaches Ollama ---------------------------------------------------


def test_the_system_prompt_is_sent_to_ollama(monkeypatch, mocker) -> None:
    bridge = _load_bridge(monkeypatch, OLLAMA_SYSTEM_PROMPT="Answer in Galician.")
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

    assert post.await_args.kwargs["json"]["system"] == "Answer in Galician."


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
    """The file ships packaged and is copied verbatim into the project: a
    syntax error would only show up when building the image, far too late."""
    with template_filesystem_path("fastapi/main.py") as path:
        compile(Path(path).read_text(encoding="utf-8"), str(path), "exec")
