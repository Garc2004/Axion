"""Tags de imagen Docker fijadas — nunca `latest` (§6.4).

No son configurables por el usuario: si lo fueran, un `axion.toml` con
`wireguard_image: ...:latest` reintroduciría exactamente el problema que
esta tabla existe para evitar (wg-easy `latest` apunta a v15, que ignora
`WG_HOST`, `PASSWORD_HASH`, `WG_DEFAULT_ADDRESS` y `WG_DEFAULT_DNS`).
"""

from __future__ import annotations

WIREGUARD_IMAGE = "ghcr.io/wg-easy/wg-easy:14"
MATTERMOST_IMAGE = "mattermost/mattermost-team-edition:10.5.1"
POSTGRES_IMAGE = "postgres:15.13-alpine"
NGINX_IMAGE = "nginx:1.27-alpine"
OLLAMA_IMAGE = "ollama/ollama:0.6.5"
#: Misma versión de Ollama compilada contra ROCm, para GPUs AMD. La imagen por
#: defecto no trae las bibliotecas de AMD: con ella, pasarle `/dev/kfd` no
#: sirve de nada y el modelo sigue corriendo en CPU sin decir por qué.
OLLAMA_ROCM_IMAGE = "ollama/ollama:0.6.5-rocm"
BACKUP_IMAGE = "offen/docker-volume-backup:v2.48.2"
N8N_IMAGE = "docker.n8n.io/n8nio/n8n:2.34.4"

ALL_PINNED_IMAGES = (
    WIREGUARD_IMAGE,
    MATTERMOST_IMAGE,
    POSTGRES_IMAGE,
    NGINX_IMAGE,
    OLLAMA_IMAGE,
    OLLAMA_ROCM_IMAGE,
    BACKUP_IMAGE,
    N8N_IMAGE,
)


def ollama_image_for(gpu_acceleration: str) -> str:
    """La imagen de Ollama que corresponde a la aceleración detectada.

    NVIDIA y CPU comparten imagen —el runtime de NVIDIA inyecta las
    bibliotecas desde el host—; AMD no, necesita una compilada contra ROCm.
    Intel no aparece porque Ollama no publica ninguna imagen para sus GPUs:
    ahí no hay nada que elegir y se corre en CPU.
    """
    from axion_wizard.detect.docker import GPU_ACCELERATION_ROCM

    return OLLAMA_ROCM_IMAGE if gpu_acceleration == GPU_ACCELERATION_ROCM else OLLAMA_IMAGE

#: prefijo de wg-easy cuyo major version 15+ reescribió la configuración por
#: variables de entorno; ver `assert_wg_easy_tag_is_safe`.
WG_EASY_REPOSITORY = "ghcr.io/wg-easy/wg-easy"
WG_EASY_MIN_SAFE_MAJOR = 14
WG_EASY_MAX_SAFE_MAJOR = 14


class UnpinnedImageError(ValueError):
    """Una imagen usa `latest` (o no tiene tag) en vez de una versión fijada."""


class UnsafeWgEasyTagError(ValueError):
    """La tag efectiva de wg-easy no es la v14 que este wizard sabe configurar."""


def split_image_tag(image: str) -> tuple[str, str | None]:
    """Separa `imagen[:tag]` respetando registros con puerto.

    No basta con partir por el último `:`: en `localhost:5000/wg-easy` ese
    `:` pertenece al puerto del registro, no a una tag. Una tag nunca
    contiene `/`, así que si lo que queda a la derecha lo tiene, no era una
    tag y la imagen está realmente sin fijar.
    """
    repo, separator, candidate = image.rpartition(":")
    if not separator or "/" in candidate:
        return image, None
    return repo, candidate


def assert_image_is_pinned(image: str) -> None:
    """Lanza `UnpinnedImageError` si `image` no trae una tag explícita distinta
    de `latest`."""
    _repo, tag = split_image_tag(image)
    if tag is None:
        raise UnpinnedImageError(f"{image!r} no tiene tag — Docker usará 'latest' implícitamente")
    if tag == "latest":
        raise UnpinnedImageError(f"{image!r} usa la tag 'latest', prohibida por la spec (§6.4)")


def parse_wg_easy_major_version(tag: str) -> int | None:
    """Extrae el major version de una tag de wg-easy (`"14"`, `"14.2"`, `"v14.0.1"`)."""
    cleaned = tag.lstrip("v")
    major_str = cleaned.split(".", 1)[0]
    try:
        return int(major_str)
    except ValueError:
        return None


def assert_wg_easy_tag_is_safe(effective_tag: str) -> None:
    """Verifica la tag *efectiva* del contenedor wg-easy ya desplegado.

    v15 es una reescritura completa que ignora `WG_HOST`, `PASSWORD_HASH`,
    `WG_DEFAULT_ADDRESS` y `WG_DEFAULT_DNS` (se configura por asistente web o
    variables `INIT_*`), rompiendo el flujo entero de forma no obvia.
    """
    if effective_tag == "latest":
        raise UnsafeWgEasyTagError(
            "wg-easy está corriendo con la tag 'latest', que en la práctica apunta a v15+"
        )
    major = parse_wg_easy_major_version(effective_tag)
    if major is None:
        raise UnsafeWgEasyTagError(
            f"no se pudo determinar el major version de la tag {effective_tag!r}"
        )
    if not (WG_EASY_MIN_SAFE_MAJOR <= major <= WG_EASY_MAX_SAFE_MAJOR):
        raise UnsafeWgEasyTagError(
            f"wg-easy {effective_tag} (v{major}) no es la v14 que este wizard sabe configurar "
            f"(WG_HOST/PASSWORD_HASH/WG_DEFAULT_ADDRESS/WG_DEFAULT_DNS dejarían de aplicarse)"
        )
