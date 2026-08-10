"""Bridge between Mattermost's outgoing webhook and Ollama's API.

Two modes, and which one is used depends on whether a bot token is set:

- **Asynchronous** (with `MM_BOT_TOKEN`): answers `200` immediately, generates
  the reply in the background and posts it to the channel through Mattermost's
  API. This is the recommended mode.
- **Synchronous** (no token): generates and returns it within the same
  request, as before. Kept so as not to break an existing install.

Why the asynchronous mode exists: Mattermost waits for the outgoing webhook's
HTTP response and abandons it after ~30 seconds
(`ServiceSettings.OutgoingIntegrationRequestsTimeout`). A 7B model on CPU
easily takes longer than that, so in synchronous mode the answer is lost
whole — the user sees the AI "not answering" and there is nothing in the logs
to explain it, because internally the model answered fine. By replying first
and posting afterwards, generation time stops having a ceiling and whatever
model the hardware can carry becomes usable.
"""

import hmac
import logging
import os

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

app = FastAPI(title="AXION FastAPI Bridge")
log = logging.getLogger("uvicorn.error")

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://ollama:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b")
# The AI's standing instructions (tone, language, what it is and what it must
# not do). Empty = the model behaves according to its own training.
# Edited with `axion-wizard model prompt "<text>"`, which also recreates this
# container — environment variables are fixed at creation time, so a `restart`
# would keep the old value.
OLLAMA_SYSTEM_PROMPT = os.environ.get("OLLAMA_SYSTEM_PROMPT", "")

#: In asynchronous mode there is no longer any need to fit inside Mattermost's
#: timeout, so this limit exists only to avoid leaving a request hanging
#: forever if Ollama gets stuck.
OLLAMA_TIMEOUT_SECONDS = float(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "600"))

# Empty by default: Mattermost generates this token when the outgoing webhook
# is created, after deployment (see .env). With it empty the bridge validates
# nothing — the same behaviour as before this field existed.
MM_WEBHOOK_TOKEN = os.environ.get("MM_WEBHOOK_TOKEN", "")

# A Mattermost bot token, so the reply can be posted once the webhook request
# has already been answered. Without it, the bridge falls back to synchronous
# mode. Configured with `axion-wizard set-bot-token <token>`.
MM_BOT_TOKEN = os.environ.get("MM_BOT_TOKEN", "")
MM_URL = os.environ.get("MM_URL", "http://mattermost:8065")
MM_API_TIMEOUT_SECONDS = 30.0

# Only matters in asynchronous mode: in synchronous mode the reply is posted by
# Mattermost's own outgoing-webhook mechanism, not by this code, and there is
# no way to ask it to hang the reply off a thread.
AI_REPLY_IN_THREAD = os.environ.get("AI_REPLY_IN_THREAD", "true").strip().lower() not in (
    "false",
    "0",
    "no",
)

TIMEOUT_MESSAGE = (
    "Model `{model}` took too long to answer. That usually means it is large for "
    "this hardware: try a smaller one with `axion-wizard model`."
)
UNREACHABLE_MESSAGE = "Could not reach Ollama ({error})."


def async_mode_enabled() -> bool:
    return bool(MM_BOT_TOKEN)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "mode": "async" if async_mode_enabled() else "sync"}


async def generate(prompt: str) -> str:
    """Ask Ollama for the answer. Returns text for the user even on failure:
    in asynchronous mode nobody else is going to see the error."""
    payload: dict = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False}
    if OLLAMA_SYSTEM_PROMPT:
        payload["system"] = OLLAMA_SYSTEM_PROMPT

    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT_SECONDS) as client:
            response = await client.post(f"{OLLAMA_HOST}/api/generate", json=payload)
            response.raise_for_status()
            return str(response.json().get("response", ""))
    except (TimeoutError, httpx.TimeoutException):
        log.warning("Ollama exceeded %ss generating with %s", OLLAMA_TIMEOUT_SECONDS, OLLAMA_MODEL)
        return TIMEOUT_MESSAGE.format(model=OLLAMA_MODEL)
    except httpx.HTTPError as exc:
        # A 500 with a traceback here surfaces in Mattermost as a broken
        # webhook with no explanation; the user has no way to know what to look
        # at.
        log.error("Error talking to Ollama: %s", exc)
        return UNREACHABLE_MESSAGE.format(error=type(exc).__name__)


async def post_to_channel(channel_id: str, message: str, root_id: str = "") -> None:
    """Post to the channel using the bot token.

    `root_id` hangs the reply off the message that triggered it, so the channel
    does not fill up with loose messages. If Mattermost rejects it — which
    happens when the original message was already part of a thread, because
    `root_id` has to be the root and not a reply — it is retried without it: a
    message outside a thread beats no message at all.
    """
    body: dict = {"channel_id": channel_id, "message": message}
    if root_id:
        body["root_id"] = root_id

    async with httpx.AsyncClient(timeout=MM_API_TIMEOUT_SECONDS) as client:
        response = await client.post(
            f"{MM_URL}/api/v4/posts",
            headers={"Authorization": f"Bearer {MM_BOT_TOKEN}"},
            json=body,
        )
        if response.status_code < 400:
            return
        if root_id:
            body.pop("root_id")
            retry = await client.post(
                f"{MM_URL}/api/v4/posts",
                headers={"Authorization": f"Bearer {MM_BOT_TOKEN}"},
                json=body,
            )
            if retry.status_code < 400:
                return
            response = retry
        log.error(
            "Mattermost rejected the post (HTTP %s): %s",
            response.status_code,
            response.text[:300],
        )


async def answer_in_background(prompt: str, channel_id: str, root_id: str) -> None:
    """Generate and post. Propagates nothing: it runs after Mattermost has
    already been answered, so an exception here would only dirty the log."""
    message = await generate(prompt)
    if not message.strip():
        return
    try:
        await post_to_channel(
            channel_id, message, root_id=root_id if AI_REPLY_IN_THREAD else ""
        )
    except httpx.HTTPError as exc:
        log.error("Could not post the reply to Mattermost: %s", exc)


@app.post("/webhook/mattermost")
async def mattermost_webhook(request: Request, background: BackgroundTasks) -> dict:
    form = await request.form()

    if MM_WEBHOOK_TOKEN:
        # compare_digest rather than `==`: an ordinary string comparison stops
        # at the first differing character, so its response time leaks how much
        # of the token was guessed correctly — a real side channel for forcing
        # it one character at a time.
        received_token = str(form.get("token", ""))
        if not hmac.compare_digest(received_token, MM_WEBHOOK_TOKEN):
            raise HTTPException(status_code=403, detail="invalid token")

    text = str(form.get("text", ""))
    channel_id = str(form.get("channel_id", ""))
    post_id = str(form.get("post_id", ""))

    if async_mode_enabled() and channel_id:
        # Answer straight away, with an empty body (Mattermost reads that as
        # "no reply to post"), and the real one arrives through the API as soon
        # as the model finishes. That way how long it takes stops mattering.
        background.add_task(answer_in_background, text, channel_id, post_id)
        return {}

    return {"text": await generate(text)}
