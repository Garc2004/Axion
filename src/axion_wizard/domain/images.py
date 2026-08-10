"""Tags de imagen Docker fijadas — nunca `latest` (§6.4).

No son configurables por el usuario: si lo fueran, un `axion.toml` con
`wireguard_image: ...:latest` dejaría al wizard configurando un wg-easy que
no es el que sabe configurar, y ese fallo es mudo — el panel arranca, y
simplemente ninguna credencial entra nunca.

Esa tag fijada apunta ahora a wg-easy **v15**. La v14 se configuraba con
`WG_HOST`/`PASSWORD_HASH`; la v15 es una reescritura completa que los ignora
y se configura con variables `INIT_*` (ver `templates/wg.env.j2`). El
guardián de abajo se mantiene, invertido: antes rechazaba v15, ahora rechaza
cualquier cosa que no lo sea.
"""

from __future__ import annotations

WIREGUARD_IMAGE = "ghcr.io/wg-easy/wg-easy:15.3.0"
#: Se queda en la serie 10.x a propósito. La 11 existe, pero subir de major
#: dispara migraciones de esquema sobre la base de datos que no se deshacen:
#: no es una actualización que un instalador deba aplicar por su cuenta a un
#: despliegue con historial de mensajes dentro.
MATTERMOST_IMAGE = "mattermost/mattermost-team-edition:10.5.1"
#: Serie 15 fijada por el mismo motivo, y con más razón: cambiar de major en
#: PostgreSQL exige `pg_upgrade` o un volcado y restauración, y el contenedor
#: se niega a arrancar sobre un directorio de datos de otra versión. Dentro
#: de la 15 sí conviene estar al día — son parches de seguridad.
POSTGRES_IMAGE = "postgres:15.18-alpine"
NGINX_IMAGE = "nginx:1.31-alpine"
OLLAMA_IMAGE = "ollama/ollama:0.32.6"
#: Misma versión de Ollama compilada contra ROCm, para GPUs AMD. La imagen por
#: defecto no trae las bibliotecas de AMD: con ella, pasarle `/dev/kfd` no
#: sirve de nada y el modelo sigue corriendo en CPU sin decir por qué.
OLLAMA_ROCM_IMAGE = "ollama/ollama:0.32.6-rocm"
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

#: Repositorio de wg-easy y el rango de major versions que este wizard sabe
#: configurar; ver `assert_wg_easy_tag_is_safe`.
#:
#: v14 y v15 no comparten *nada* de su configuración: la v14 leía
#: `WG_HOST`/`PASSWORD_HASH` (bcrypt) y la v15 usa `INIT_*` con la contraseña
#: en claro, usuario incluido, y solo en el primer arranque. Aceptar las dos
#: significaría mantener dos clientes de API y dos plantillas de `wg.env`
#: para un panel que el usuario ve una vez; se fija una sola.
WG_EASY_REPOSITORY = "ghcr.io/wg-easy/wg-easy"
WG_EASY_MIN_SAFE_MAJOR = 15
WG_EASY_MAX_SAFE_MAJOR = 15


class UnpinnedImageError(ValueError):
    """Una imagen usa `latest` (o no tiene tag) en vez de una versión fijada."""


class UnsafeWgEasyTagError(ValueError):
    """La tag efectiva de wg-easy no es la v15 que este wizard sabe configurar."""


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

    Cada major version de wg-easy se configura de una forma incompatible con
    la anterior, y equivocarse no da error: el panel arranca, responde, y lo
    único que pasa es que las credenciales que el wizard configuró no sirven
    —o, en una v14, que la API a la que llama no existe—. De ahí que se
    compruebe la tag del contenedor en marcha y no solo la escrita en el
    `docker-compose.yml`, que cualquiera puede haber editado a mano.
    """
    if effective_tag == "latest":
        raise UnsafeWgEasyTagError(
            "wg-easy está corriendo con la tag 'latest': lo que apunte hoy puede "
            "dejar de ser la v15 sin previo aviso"
        )
    major = parse_wg_easy_major_version(effective_tag)
    if major is None:
        raise UnsafeWgEasyTagError(
            f"no se pudo determinar el major version de la tag {effective_tag!r}"
        )
    if major < WG_EASY_MIN_SAFE_MAJOR:
        raise UnsafeWgEasyTagError(
            f"wg-easy {effective_tag} (v{major}) es anterior a la v15 que este wizard "
            "configura: la v14 espera WG_HOST/PASSWORD_HASH y expone otra API "
            "(/api/wireguard/client), así que ni las credenciales ni el alta de "
            "clientes funcionarían"
        )
    if major > WG_EASY_MAX_SAFE_MAJOR:
        raise UnsafeWgEasyTagError(
            f"wg-easy {effective_tag} (v{major}) es posterior a la v15 que este wizard "
            "configura: cada major cambia su configuración por completo, y el fallo "
            "sería mudo — el panel arranca y ninguna credencial entra"
        )
