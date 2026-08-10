"""Puente entre el webhook saliente de Mattermost y la API de Ollama.

Dos modos, y el que se use depende de si hay un token de bot configurado:

- **Asíncrono** (con `MM_BOT_TOKEN`): se responde `200` de inmediato, se
  genera la respuesta en segundo plano y se publica en el canal por la API
  de Mattermost. Es el modo recomendado.
- **Síncrono** (sin token): se genera y se devuelve en la misma petición,
  como antes. Se conserva para no romper una instalación existente.

Por qué existe el modo asíncrono: Mattermost espera la respuesta HTTP del
webhook saliente y la abandona a los ~30 segundos
(`ServiceSettings.OutgoingIntegrationRequestsTimeout`). Un modelo de 7B en
CPU tarda más que eso con facilidad, así que en modo síncrono la respuesta
se pierde entera — el usuario ve que la IA "no contesta" y no hay nada en
los logs que lo explique, porque por dentro el modelo respondió bien.
Respondiendo primero y publicando después, el tiempo de generación deja de
tener techo y se puede usar el modelo que el hardware aguante.
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
# Instrucciones permanentes de la IA (tono, idioma, qué es y qué no debe
# hacer). Vacío = el modelo se comporta según su propio entrenamiento.
# Se edita con `axion-wizard model prompt "<texto>"`, que además recrea este
# contenedor — las variables de entorno se fijan al crearlo, así que un
# `restart` conservaría el valor viejo.
OLLAMA_SYSTEM_PROMPT = os.environ.get("OLLAMA_SYSTEM_PROMPT", "")

#: En modo asíncrono ya no hay que caber en el timeout de Mattermost, así
#: que el límite solo existe para no dejar una petición colgada para siempre
#: si Ollama se queda bloqueado.
OLLAMA_TIMEOUT_SECONDS = float(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "600"))

# Vacío por defecto: Mattermost genera este token al crear el webhook
# saliente, después del despliegue (ver .env). Con él vacío el puente no
# valida nada — mismo comportamiento que antes de este campo.
MM_WEBHOOK_TOKEN = os.environ.get("MM_WEBHOOK_TOKEN", "")

# Token de un bot de Mattermost, para poder publicar la respuesta cuando ya
# se ha contestado a la petición del webhook. Sin él, el puente cae al modo
# síncrono. Se configura con `axion-wizard set-bot-token <token>`.
MM_BOT_TOKEN = os.environ.get("MM_BOT_TOKEN", "")
MM_URL = os.environ.get("MM_URL", "http://mattermost:8065")
MM_API_TIMEOUT_SECONDS = 30.0

TIMEOUT_MESSAGE = (
    "El modelo `{model}` tardó demasiado en responder. Suele significar que es "
    "grande para este hardware: probar uno más pequeño con `axion-wizard model`."
)
UNREACHABLE_MESSAGE = "No se pudo contactar con Ollama ({error})."


def async_mode_enabled() -> bool:
    return bool(MM_BOT_TOKEN)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "mode": "async" if async_mode_enabled() else "sync"}


async def generate(prompt: str) -> str:
    """Pide la respuesta a Ollama. Devuelve un texto para el usuario incluso
    cuando falla: en modo asíncrono nadie más va a ver el error."""
    payload: dict = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False}
    if OLLAMA_SYSTEM_PROMPT:
        payload["system"] = OLLAMA_SYSTEM_PROMPT

    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT_SECONDS) as client:
            response = await client.post(f"{OLLAMA_HOST}/api/generate", json=payload)
            response.raise_for_status()
            return str(response.json().get("response", ""))
    except (TimeoutError, httpx.TimeoutException):
        log.warning("Ollama excedió %ss generando con %s", OLLAMA_TIMEOUT_SECONDS, OLLAMA_MODEL)
        return TIMEOUT_MESSAGE.format(model=OLLAMA_MODEL)
    except httpx.HTTPError as exc:
        # Un 500 con traceback aquí sale en Mattermost como un webhook roto y
        # sin explicación; el usuario no tiene forma de saber qué mirar.
        log.error("Error hablando con Ollama: %s", exc)
        return UNREACHABLE_MESSAGE.format(error=type(exc).__name__)


async def post_to_channel(channel_id: str, message: str, root_id: str = "") -> None:
    """Publica en el canal con el token del bot.

    `root_id` cuelga la respuesta del mensaje que la disparó, para no llenar
    el canal de mensajes sueltos. Si Mattermost lo rechaza —pasa cuando el
    mensaje original ya era parte de un hilo, porque `root_id` tiene que ser
    la raíz y no una respuesta— se reintenta sin él: mejor un mensaje fuera
    de hilo que ninguno.
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
            "Mattermost rechazó la publicación (HTTP %s): %s",
            response.status_code,
            response.text[:300],
        )


async def answer_in_background(prompt: str, channel_id: str, root_id: str) -> None:
    """Genera y publica. No propaga nada: corre después de haber respondido
    a Mattermost, así que una excepción aquí solo ensuciaría el log."""
    message = await generate(prompt)
    if not message.strip():
        return
    try:
        await post_to_channel(channel_id, message, root_id=root_id)
    except httpx.HTTPError as exc:
        log.error("No se pudo publicar la respuesta en Mattermost: %s", exc)


@app.post("/webhook/mattermost")
async def mattermost_webhook(request: Request, background: BackgroundTasks) -> dict:
    form = await request.form()

    if MM_WEBHOOK_TOKEN:
        # compare_digest en vez de `==`: una comparación normal de strings
        # corta en el primer carácter distinto, así que su tiempo de
        # respuesta filtra cuánto del token se acertó — un side-channel
        # real para forzarlo carácter a carácter.
        received_token = str(form.get("token", ""))
        if not hmac.compare_digest(received_token, MM_WEBHOOK_TOKEN):
            raise HTTPException(status_code=403, detail="token inválido")

    text = str(form.get("text", ""))
    channel_id = str(form.get("channel_id", ""))
    post_id = str(form.get("post_id", ""))

    if async_mode_enabled() and channel_id:
        # Se contesta ya, con el cuerpo vacío (Mattermost lo interpreta como
        # "sin respuesta que publicar"), y la de verdad llega por la API en
        # cuanto el modelo termine. Así deja de importar cuánto tarde.
        background.add_task(answer_in_background, text, channel_id, post_id)
        return {}

    return {"text": await generate(text)}
