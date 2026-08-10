"""Paso 5 — Renderizado del compose y archivos de configuración (§4.5).

Orden de responsabilidades:
1. Renderizar `docker-compose.yml`, `.env`, `wg.env` y el config de nginx
   desde las plantillas Jinja2 empaquetadas (`utils.resources`).
2. Si `docker-compose.yml` ya existe, hacer backup con timestamp y editarlo
   con `ruamel.yaml` en vez de sobrescribirlo, preservando cualquier
   personalización del usuario fuera de los servicios que gestiona el wizard.
3. Validar la forma del YAML resultante y que la variable SSRF esté presente.
4. Escribir `.env`/`wg.env` con permisos restringidos al usuario actual.
"""

from __future__ import annotations

import datetime
import io
import os
from collections.abc import MutableMapping
from pathlib import Path

import jinja2
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from axion_wizard import images
from axion_wizard.config import AxionConfig
from axion_wizard.console import console
from axion_wizard.detect.docker import GPU_ACCELERATION_NONE
from axion_wizard.errors import ConfigError
from axion_wizard.services.compose import config_validate
from axion_wizard.stack import MANAGED_SERVICES
from axion_wizard.steps.base import Step, StepResult
from axion_wizard.utils import secrets as secret_utils
from axion_wizard.utils.fsperms import restrict_to_owner
from axion_wizard.utils.resources import read_template_text

SSRF_ENV_KEY = "MM_SERVICESETTINGS_ALLOWEDUNTRUSTEDINTERNALCONNECTIONS"
#: `n8n:5678` va siempre, aunque n8n no esté instalado: la lista solo permite
#: destinos, no exige que existan, y el coste de que sobre una entrada es
#: cero. El de que falte no: la protección SSRF descarta el webhook saliente
#: **en silencio**, sin error en ningún log, y desde fuera parece que n8n
#: simplemente no se entera de nada.
SSRF_ENV_VALUE = "fastapi:8000 fastapi n8n:5678 n8n"

#: Carpeta donde el servicio `backup` deja los tar.gz, relativa al proyecto.
BACKUPS_RELATIVE_DIR = Path("backups")

#: De madrugada porque la copia para PostgreSQL y Mattermost mientras archiva.
DEFAULT_BACKUP_CRON_EXPRESSION = "0 3 * * *"
DEFAULT_BACKUP_RETENTION_DAYS = "7"

#: Segundos que Mattermost espera a que el webhook saliente conteste. El valor
#: por defecto de Mattermost son 30, que en CPU no le llegan ni a un modelo
#: pequeño: pasado el plazo la respuesta se pierde entera y sin error. 180
#: cubre con holgura una respuesta larga de un modelo de 3B en CPU.
OUTGOING_WEBHOOK_TIMEOUT_SECONDS = "180"

#: Zona horaria de n8n, para que sus cron disparen a la hora que el usuario
#: espera. No se deduce del equipo a propósito: n8n quiere un nombre IANA
#: (`America/Argentina/Buenos_Aires`) y Windows da los suyos («Hora est. de
#: Argentina»), que n8n no entiende. Antes que colar un valor que falle en
#: silencio, se deja en UTC y se documenta en el `.env`.
DEFAULT_N8N_TIMEZONE = "UTC"

ZONE_IDENTIFIER_SUFFIX = ":Zone.Identifier"

#: Prefijo del nombre de proyecto generado para una instalación nueva. Ver
#: `resolve_compose_project_name`.
PROJECT_NAME_PREFIX = "axion"


def _legacy_project_name_from_compose(project_dir: Path) -> str | None:
    """El `name:` de nivel superior de un `docker-compose.yml` ya existente,
    si lo tiene.

    Solo lo escribían las versiones del wizard anteriores a que el nombre de
    proyecto se moviera a `.env` (fijaban `name: axion`, igual para *todas*
    las instalaciones — la causa del bug que `resolve_compose_project_name`
    existe para evitar). Sirve únicamente de migración hacia atrás: para un
    despliegue con ese `docker-compose.yml`, hay que conservar el mismo
    nombre de proyecto o Compose dejaría de encontrar sus contenedores y
    volúmenes ya existentes.
    """
    compose_path = project_dir / "docker-compose.yml"
    try:
        text = compose_path.read_text(encoding="utf-8")
    except OSError:
        return None
    yaml = YAML(typ="safe")
    try:
        data = yaml.load(text)
    except YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    name = data.get("name")
    return name if isinstance(name, str) and name else None


def resolve_compose_project_name(project_dir: Path) -> str:
    """Nombre de proyecto de Compose para este despliegue: estable y, sobre
    todo, único.

    Se lee de un `.env` ya existente si lo hay, para sobrevivir a cualquier
    `install` posterior. A falta de eso, se migra el `name:` de un
    `docker-compose.yml` de una versión anterior del wizard si lo encuentra
    (ver `_legacy_project_name_from_compose`). Solo si no hay ninguna pista
    —instalación de verdad nueva— se genera uno con un sufijo aleatorio.

    El sufijo es lo que importa: sin él, *todas* las instalaciones de
    axion-wizard en el mismo host Docker comparten el nombre de proyecto
    `axion`, y Compose identifica un proyecto por su nombre, no por la
    carpeta desde la que se invoca — así que comparten también sus
    contenedores y volúmenes. Es un incidente real, no hipotético: instalar
    en una segunda carpeta generó una contraseña de PostgreSQL nueva y la
    escribió en un `.env` que, sin saberlo, apuntaba al mismo proyecto que un
    despliegue ya en marcha con datos reales. Docker aceptó la nueva
    contraseña sin quejarse —solo se aplica al inicializar un volumen vacío,
    y este ya no lo estaba— y Mattermost quedó autenticando contra la
    contraseña vieja con la nueva escrita en su `.env`: bucle de reinicio,
    sin que ningún log señalara que el problema era una colisión entre dos
    instalaciones distintas.
    """
    existing = existing_env_value(project_dir, "COMPOSE_PROJECT_NAME")
    if existing:
        return existing
    legacy = _legacy_project_name_from_compose(project_dir)
    if legacy:
        return legacy
    return f"{PROJECT_NAME_PREFIX}-{secret_utils.generate_hex_secret(4)}"


def _render(template_name: str, context: dict) -> str:
    template_text = read_template_text(template_name)
    template = jinja2.Template(
        template_text, keep_trailing_newline=True, undefined=jinja2.StrictUndefined
    )
    return template.render(**context)


def build_compose_context(
    config: AxionConfig,
    gpu_acceleration: str = GPU_ACCELERATION_NONE,
) -> dict:
    return {
        "n8n_image": images.N8N_IMAGE,
        "ssrf_allowed_connections": SSRF_ENV_VALUE,
        "host": config.host,
        "wireguard_variant": config.wireguard_variant.value,
        "postgres_image": images.POSTGRES_IMAGE,
        "mattermost_image": images.MATTERMOST_IMAGE,
        "ollama_image": images.ollama_image_for(gpu_acceleration),
        "nginx_image": images.NGINX_IMAGE,
        "wireguard_image": images.WIREGUARD_IMAGE,
        "backup_image": images.BACKUP_IMAGE,
        "gpu_acceleration": gpu_acceleration,
        "outgoing_webhook_timeout": OUTGOING_WEBHOOK_TIMEOUT_SECONDS,
    }


def render_compose(config: AxionConfig, gpu_acceleration: str = GPU_ACCELERATION_NONE) -> str:
    return _render("docker-compose.yml.j2", build_compose_context(config, gpu_acceleration))


#: Claves de `.env` que el usuario rellena *después* del despliegue y que
#: por tanto hay que arrastrar de la corrida anterior en vez de regenerar.
#: `POSTGRES_PASSWORD` no está aquí porque viaja por `AxionConfig` (el paso 3
#: ya lo lee del `.env` existente, ver `s03_config._existing_postgres_password`).
PRESERVED_ENV_KEYS = (
    "MM_WEBHOOK_TOKEN",
    "MM_BOT_TOKEN",
    "OLLAMA_SYSTEM_PROMPT",
    "BACKUP_CRON_EXPRESSION",
    "BACKUP_RETENTION_DAYS",
    # Regenerarla deja ilegibles TODAS las credenciales guardadas en n8n, sin
    # posibilidad de recuperarlas y sin que nada avise hasta que un flujo
    # falla al autenticarse.
    "N8N_ENCRYPTION_KEY",
    "N8N_TIMEZONE",
    # Solo tiene efecto en modo asíncrono (con MM_BOT_TOKEN puesto); se
    # pregunta junto con el token del bot en el paso 9.
    "AI_REPLY_IN_THREAD",
)

#: Regenerar el .env sin que el usuario haya elegido nunca no debe cambiar
#: el comportamiento que ya tenía desplegado.
DEFAULT_AI_REPLY_IN_THREAD = "true"


def existing_env_value(project_dir: Path, key: str) -> str | None:
    """Valor de `key` en el `.env` de una corrida anterior, si lo hay.

    Único punto de lectura de valores ya escritos, para que quien regenere
    el `.env` no tenga que saber cómo se parsea.

    Un archivo ilegible se trata como "no hay valor previo" en vez de
    reventar: el wizard siempre escribe UTF-8, pero `OLLAMA_SYSTEM_PROMPT`
    invita a editarlo a mano, y un Notepad guardando en ANSI produce bytes
    que `dotenv_values` no sabe decodificar. Perder un valor que ya no se
    puede leer es malo; abortar la instalación entera por ello, peor.
    """
    from dotenv import dotenv_values

    try:
        return dotenv_values(project_dir / ".env").get(key) or None
    except (OSError, UnicodeDecodeError):
        return None


def preserved_env_values(project_dir: Path) -> dict[str, str]:
    """Los valores de `PRESERVED_ENV_KEYS` que ya estén escritos en `.env`."""
    return {key: existing_env_value(project_dir, key) or "" for key in PRESERVED_ENV_KEYS}


def render_env(
    config: AxionConfig, compose_project_name: str, preserved: dict[str, str] | None = None
) -> str:
    """Renderiza `.env`.

    `compose_project_name` es obligatorio y sin valor por defecto **a
    propósito**: es lo que evita que dos instalaciones distintas en el mismo
    host Docker terminen compartiendo contenedores y volúmenes (incidente
    real — ver `resolve_compose_project_name`, que es quien debe calcularlo
    antes de llamar aquí). Un valor por defecto fijo reintroduciría
    exactamente ese bug en cualquier llamada que se olvidara de pasarlo.

    `preserved` trae los valores que el usuario configura *después* del
    despliegue y que este archivo no puede conocer de antemano: el token del
    webhook (Mattermost lo genera al crearlo) y las instrucciones de la IA.
    Regenerando el archivo entero con los valores por defecto, una segunda
    pasada de `install` —para cambiar el modelo, por ejemplo— los borraba sin
    decir nada; en el caso del token eso significaba además que fastapi
    volvía a aceptar cualquier llamada al webhook sin validarla. Una
    degradación de seguridad invisible: ni error, ni aviso, ni nada en logs.
    """
    preserved = preserved or {}
    return _render(
        "env.j2",
        {
            "compose_project_name": compose_project_name,
            "postgres_password": config.postgres_password.get_secret_value(),
            "ollama_model": config.ollama_model,
            "host": config.host,
            "mm_webhook_token": preserved.get("MM_WEBHOOK_TOKEN", ""),
            "mm_bot_token": preserved.get("MM_BOT_TOKEN", ""),
            "ollama_system_prompt": preserved.get("OLLAMA_SYSTEM_PROMPT", ""),
            # A diferencia de los tokens, aquí el vacío no es un valor válido:
            # Compose lo interpolaría tal cual y el servicio arrancaría sin
            # horario ni retención. De ahí el `or` con el valor por defecto,
            # que cubre tanto la primera instalación como un `.env` al que le
            # hayan borrado la línea.
            "backup_cron_expression": (
                preserved.get("BACKUP_CRON_EXPRESSION") or DEFAULT_BACKUP_CRON_EXPRESSION
            ),
            "backup_retention_days": (
                preserved.get("BACKUP_RETENTION_DAYS") or DEFAULT_BACKUP_RETENTION_DAYS
            ),
            # Se genera una sola vez, la primera. A partir de ahí llega por
            # `preserved` y no se vuelve a tocar nunca.
            "n8n_encryption_key": (
                preserved.get("N8N_ENCRYPTION_KEY") or secret_utils.generate_hex_secret()
            ),
            "n8n_timezone": preserved.get("N8N_TIMEZONE") or DEFAULT_N8N_TIMEZONE,
            "ai_reply_in_thread": (
                preserved.get("AI_REPLY_IN_THREAD") or DEFAULT_AI_REPLY_IN_THREAD
            ),
        },
    )


def render_wg_env(config: AxionConfig) -> str:
    return _render(
        "wg.env.j2",
        {
            "host": config.host,
            "wireguard_admin_password_hash": (
                config.wireguard_admin_password_hash.get_secret_value()
            ),
        },
    )


def render_nginx_conf(config: AxionConfig) -> str:
    return _render("nginx-mattermost.conf.j2", {"host": config.host})


def validate_compose_yaml_shape(compose_text: str) -> None:
    """Valida que el YAML generado sea sintácticamente correcto y tenga la
    forma mínima esperada. La validación semántica real
    (`docker compose config --quiet`) requiere Docker y se ejecuta en el
    paso 6 vía `services.compose`."""
    yaml = YAML(typ="safe")
    data = yaml.load(compose_text)
    if not isinstance(data, dict) or "services" not in data:
        raise ConfigError(
            what="El docker-compose.yml generado no tiene la forma esperada",
            why="Falta la clave `services` de nivel superior; el archivo generado está corrupto.",
            steps=["Reportar este error — es un bug del wizard, no una acción del usuario."],
        )
    services = data["services"]
    missing = [name for name in MANAGED_SERVICES if name not in services]
    if missing:
        raise ConfigError(
            what=f"Faltan servicios gestionados en docker-compose.yml: {', '.join(missing)}",
            why="El archivo generado está incompleto y el despliegue fallaría.",
            steps=["Reportar este error — es un bug del wizard, no una acción del usuario."],
        )


def assert_ssrf_env_present(compose_text: str) -> None:
    """El usuario no debe tener que saber que esta variable existe (§12): el
    wizard la inyecta siempre y aborta si por algún motivo no quedó."""
    if SSRF_ENV_VALUE not in compose_text:
        raise ConfigError(
            what="Falta la variable de protección SSRF en el servicio mattermost",
            why=(
                f'Sin `{SSRF_ENV_KEY}: "{SSRF_ENV_VALUE}"`, la protección SSRF de '
                "Mattermost bloquea el webhook hacia fastapi:8000 de forma silenciosa: "
                "no aparece error en ningún log, el webhook simplemente nunca dispara."
            ),
            steps=["Reportar este error — es un bug del wizard, no una acción del usuario."],
        )


def assert_no_unpinned_images(compose_text: str) -> None:
    for image in images.ALL_PINNED_IMAGES:
        images.assert_image_is_pinned(image)
    if ":latest" in compose_text:
        raise ConfigError(
            what="El docker-compose.yml generado referencia una imagen con tag `latest`",
            why=(
                "Las tags fijadas existen para builds reproducibles y para evitar "
                "romper wg-easy (§6.4)."
            ),
            steps=["Reportar este error — es un bug del wizard, no una acción del usuario."],
        )


def backup_existing_file(path: Path) -> Path | None:
    """Si `path` ya existe, lo copia con sufijo de timestamp antes de tocarlo.

    El timestamp tiene resolución de segundo, así que dos backups seguidos
    (un `install` reintentado, por ejemplo) caerían en el mismo nombre y el
    segundo pisaría al primero — justo la copia que este mecanismo existe
    para conservar. Si el nombre ya está tomado se añade un contador.
    """
    if not path.exists():
        return None
    timestamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")

    backup_path = path.with_name(f"{path.name}.{timestamp}.bak")
    counter = 2
    while backup_path.exists():
        backup_path = path.with_name(f"{path.name}.{timestamp}-{counter}.bak")
        counter += 1

    backup_path.write_bytes(path.read_bytes())
    return backup_path


def merge_compose_preserving_user_edits(existing_text: str, rendered_text: str) -> str:
    """Reemplaza solo los servicios de `MANAGED_SERVICES` y las claves
    `volumes`/`networks` que el wizard gestiona, preservando cualquier otro
    servicio, clave de nivel superior o comentario que el usuario haya
    añadido a mano al `docker-compose.yml` existente."""
    yaml = YAML()
    yaml.preserve_quotes = True

    try:
        existing = yaml.load(existing_text)
    except YAMLError as exc:
        raise ConfigError(
            what="El docker-compose.yml existente no es YAML válido",
            why=(
                "El wizard hace backup y fusiona el archivo existente en vez de "
                f"sobrescribirlo, pero no pudo parsearlo: {exc}"
            ),
            steps=[
                "Corregir la sintaxis del docker-compose.yml actual, o",
                "Moverlo/renombrarlo para que el wizard genere uno limpio.",
            ],
        ) from exc

    rendered = yaml.load(rendered_text)

    if existing is None:
        existing = {}
    # Un compose válido es siempre un mapping en la raíz. Si no lo es (una
    # lista, un escalar...), indexarlo más abajo reventaría con un TypeError
    # crudo en vez de un mensaje accionable.
    if not isinstance(existing, MutableMapping):
        raise ConfigError(
            what="El docker-compose.yml existente no tiene un mapping en la raíz",
            why=(
                f"Se encontró {type(existing).__name__} donde Compose espera un "
                "mapping con claves como `services:`. El archivo no es un "
                "docker-compose.yml válido y no se puede fusionar con seguridad."
            ),
            steps=[
                "Revisar el docker-compose.yml actual, o",
                "Moverlo/renombrarlo para que el wizard genere uno limpio.",
            ],
        )

    if not isinstance(existing.get("services"), MutableMapping):
        existing["services"] = {}

    for name in MANAGED_SERVICES:
        if name in rendered.get("services", {}):
            existing["services"][name] = rendered["services"][name]

    for top_level_key in ("volumes", "networks"):
        rendered_section = rendered.get(top_level_key)
        if rendered_section is None:
            continue
        if not isinstance(existing.get(top_level_key), MutableMapping):
            existing[top_level_key] = rendered_section
        else:
            for key, value in rendered_section.items():
                existing[top_level_key].setdefault(key, value)

    buffer = io.StringIO()
    yaml.dump(existing, buffer)
    return buffer.getvalue()


def render_compose_to_disk(
    config: AxionConfig, compose_path: Path, gpu_acceleration: str = GPU_ACCELERATION_NONE
) -> Path | None:
    """Renderiza `docker-compose.yml`, haciendo backup + merge si ya existía.
    Devuelve la ruta del backup creado, o `None` si el archivo era nuevo."""
    rendered = render_compose(config, gpu_acceleration=gpu_acceleration)
    validate_compose_yaml_shape(rendered)
    assert_ssrf_env_present(rendered)
    assert_no_unpinned_images(rendered)

    backup_path = backup_existing_file(compose_path)
    if backup_path is not None:
        existing_text = backup_path.read_text(encoding="utf-8")
        final_text = merge_compose_preserving_user_edits(existing_text, rendered)
    else:
        final_text = rendered

    compose_path.parent.mkdir(parents=True, exist_ok=True)
    compose_path.write_text(final_text, encoding="utf-8")
    return backup_path


def write_secret_env_file(path: Path, content: str, *, backup: bool = False) -> Path | None:
    """Escribe un archivo con secretos (`.env`, `wg.env`) restringido al
    usuario actual (§4.5, §9). Devuelve la ruta del backup, si se pidió y
    había algo que respaldar.

    `backup` existe porque estos dos archivos se regeneran por completo en
    cada `install` y son justo donde el usuario acaba tocando cosas a mano
    (un `MM_WEBHOOK_TOKEN`, un ajuste de wg-easy). El `docker-compose.yml` ya
    se respaldaba; estos no, y eran los únicos con secretos dentro.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = backup_existing_file(path) if backup else None
    path.write_text(content, encoding="utf-8")
    restrict_to_owner(path)
    if backup_path is not None:
        # El backup hereda los secretos del original, así que sus permisos
        # también: si no, `.env` queda restringido y su copia al lado no.
        restrict_to_owner(backup_path)
    return backup_path


def update_env_value(path: Path, key: str, value: str) -> None:
    """Actualiza (o añade) una clave en un `.env` ya existente, preservando
    el resto de líneas —comentarios, orden, otras claves— intactas.

    Existe para valores que no se conocen al desplegar y se rellenan
    después a mano, como `MM_WEBHOOK_TOKEN` (Mattermost lo genera al crear
    el webhook saliente, ya con el stack corriendo — §4.5 no puede
    escribirlo por adelantado). Reescribir el archivo entero desde cero
    perdería cualquier ajuste manual que el usuario haya hecho.
    """
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    prefix = f"{key}="
    new_line = f"{prefix}{value}"

    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = new_line
            break
    else:
        lines.append(new_line)

    write_secret_env_file(path, "\n".join(lines) + "\n")


def ensure_gitignore_entries(project_dir: Path) -> bool:
    """Añade `.env`, `wg.env`, `nginx/certs/` y `.axion-wizard-state.json` al
    `.gitignore` si existe repositorio. Devuelve `True` si se modificó algo."""
    git_dir = project_dir / ".git"
    if not git_dir.exists():
        return False

    # `backups/` lleva volcados de la base de datos y las claves de WireGuard:
    # subirlo a un repositorio filtraría todo lo que `.env` protege.
    required_entries = [
        ".env",
        "wg.env",
        "nginx/certs/",
        ".axion-wizard-state.json",
        "backups/",
    ]
    gitignore_path = project_dir / ".gitignore"
    existing_lines = (
        gitignore_path.read_text(encoding="utf-8").splitlines() if gitignore_path.exists() else []
    )
    existing_set = {line.strip() for line in existing_lines}

    missing_entries = [entry for entry in required_entries if entry not in existing_set]
    if not missing_entries:
        return False

    with gitignore_path.open("a", encoding="utf-8") as handle:
        if existing_lines and existing_lines[-1].strip():
            handle.write("\n")
        handle.write("# añadido por axion-wizard\n")
        for entry in missing_entries:
            handle.write(f"{entry}\n")
    return True


#: Archivos del puente FastAPI que el compose construye desde `./fastapi`
#: (`build.context`). Van empaquetados en el binario y se copian tal cual.
FASTAPI_TEMPLATE_FILES = ("Dockerfile", "main.py", "requirements.txt")

NGINX_CONF_RELATIVE_PATH = Path("nginx") / "nginx-mattermost.conf"


def write_fastapi_sources(project_dir: Path) -> list[Path]:
    """Vuelca `templates/fastapi/` en `<project_dir>/fastapi/`.

    El compose declara `build.context: ./fastapi`, así que sin estos archivos
    `docker compose up --build` falla con un error de contexto que no dice
    nada sobre lo que realmente pasa (que el wizard no los escribió).
    """
    target_dir = project_dir / "fastapi"
    target_dir.mkdir(parents=True, exist_ok=True)

    written = []
    for filename in FASTAPI_TEMPLATE_FILES:
        content = read_template_text(f"fastapi/{filename}")
        path = target_dir / filename
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


#: Directorios que no se recorren buscando `:Zone.Identifier`. Ninguno puede
#: contener uno que importe, y `.venv`/`node_modules` tienen decenas de miles
#: de archivos: recorrerlos convertía una limpieza instantánea en una pausa
#: de varios segundos en mitad del paso que escribe todo.
_UNSCANNED_DIRECTORY_NAMES = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}
)


def clean_zone_identifier_files(project_dir: Path) -> tuple[list[Path], list[Path]]:
    """Borra archivos `*:Zone.Identifier` (metadato de "descargado de
    Internet" que Windows deja en archivos copiados desde fuera de WSL).

    Devuelve `(borrados, no_borrados)`. Un archivo que no se deja borrar
    —bloqueado por otro proceso, sin permisos— **no** aborta el paso: es
    basura cosmética, y hacer caer aquí el paso que escribe el compose, el
    `.env` y el certificado sería desproporcionado. Antes el `OSError` subía
    hasta el manejador genérico y salía como `Error inesperado`.

    Usa `os.walk` en vez de `Path.glob`/`rglob`: en pathlib, un patrón con
    `:` se interpreta como prefijo de unidad (`C:`) y `glob("*:Zone.Identifier")`
    lanza `NotImplementedError: Non-relative patterns are unsupported`.
    """
    removed: list[Path] = []
    failed: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(project_dir):
        # Poda in-place: `os.walk` respeta la mutación de `dirnames`.
        dirnames[:] = [name for name in dirnames if name not in _UNSCANNED_DIRECTORY_NAMES]
        for filename in filenames:
            if not filename.endswith(ZONE_IDENTIFIER_SUFFIX):
                continue
            path = Path(dirpath) / filename
            try:
                path.unlink()
            except OSError:
                failed.append(path)
            else:
                removed.append(path)
    return removed, failed


class ComposeStep(Step):
    """Escribe todos los artefactos del proyecto y valida el compose (§4.5).

    Es el primer paso que toca el disco: hasta aquí el flujo solo ha mirado
    y preguntado. De ahí que la confirmación del paso 3 sea el punto de no
    retorno, y que este paso haga backup de lo que ya existiera.
    """

    name = "compose"
    title = "Compose y archivos de configuración"

    def run(self) -> StepResult:
        config = self.context.require_config()
        environment = self.context.require_environment()
        compose_path = self.context.project_dir / "docker-compose.yml"

        if self.state.dry_run:
            self._announce_dry_run(compose_path)
            return StepResult(name=self.name, ok=True, message="omitido por --dry-run")

        # Antes de que render_compose_to_disk sobrescriba docker-compose.yml:
        # la migración desde una versión antigua del wizard lee el `name:`
        # de ese archivo tal como está *ahora*, no del que se está por escribir.
        project_name = resolve_compose_project_name(self.context.project_dir)

        backup_path = render_compose_to_disk(
            config, compose_path, gpu_acceleration=environment.gpu_acceleration
        )

        # Los valores que el usuario rellena después del despliegue (token del
        # webhook, instrucciones de la IA) se arrastran de la corrida anterior:
        # regenerar `.env` con los valores por defecto los borraba en silencio.
        preserved = preserved_env_values(self.context.project_dir)
        write_secret_env_file(
            self.context.project_dir / ".env",
            render_env(config, project_name, preserved=preserved),
            backup=True,
        )
        write_secret_env_file(
            self.context.project_dir / "wg.env", render_wg_env(config), backup=True
        )
        kept = [key for key, value in preserved.items() if value]
        if kept:
            console.print(f"[axion.dim]Conservado del .env anterior: {', '.join(kept)}.[/]")

        nginx_conf_path = self.context.project_dir / NGINX_CONF_RELATIVE_PATH
        nginx_conf_path.parent.mkdir(parents=True, exist_ok=True)
        nginx_conf_path.write_text(render_nginx_conf(config), encoding="utf-8")

        write_fastapi_sources(self.context.project_dir)
        # Se crea aquí y no se deja a Docker: un bind mount hacia una ruta que
        # no existe la crea `root`, y entonces las copias las escribe root en
        # una carpeta que el usuario no puede leer ni borrar.
        (self.context.project_dir / BACKUPS_RELATIVE_DIR).mkdir(parents=True, exist_ok=True)
        ensure_gitignore_entries(self.context.project_dir)

        if environment.wsl.inside_wsl:
            removed, failed = clean_zone_identifier_files(self.context.project_dir)
            if removed:
                console.print(
                    f"[axion.dim]Limpiados {len(removed)} archivos :Zone.Identifier.[/]"
                )
            if failed:
                console.print(
                    f"[axion.dim]{len(failed)} archivos :Zone.Identifier no se pudieron "
                    "borrar (bloqueados o sin permisos); no afecta al despliegue.[/]"
                )

        # Validación semántica real, con Docker: la de forma ya la hizo
        # `render_compose_to_disk` sobre el texto renderizado.
        config_validate(compose_path)

        if backup_path is not None:
            console.print(f"[axion.info]Compose anterior respaldado en:[/] {backup_path}")
        console.print(f"[axion.ok]Archivos escritos en:[/] {self.context.project_dir}")
        console.print(
            f"[axion.dim]Nombre de proyecto de Compose: {project_name} "
            "(prefija contenedores y volúmenes; no compartir con otro despliegue).[/]"
        )

        return StepResult(
            name=self.name,
            ok=True,
            data={"backup": str(backup_path) if backup_path else ""},
            message=f"compose y .env generados en {self.context.project_dir}",
        )

    def verify(self) -> StepResult:
        if self.state.dry_run:
            return StepResult(name=self.name, ok=True, message="omitido por --dry-run")

        missing = [
            str(relative)
            for relative in (
                Path("docker-compose.yml"),
                Path(".env"),
                Path("wg.env"),
                NGINX_CONF_RELATIVE_PATH,
                Path("fastapi") / "Dockerfile",
            )
            if not (self.context.project_dir / relative).exists()
        ]
        if missing:
            return StepResult(
                name=self.name, ok=False, message=f"faltan archivos: {', '.join(missing)}"
            )
        return StepResult(name=self.name, ok=True, message="todos los archivos presentes")

    def _announce_dry_run(self, compose_path: Path) -> None:
        console.print(f"[axion.info][dry-run][/] escribiría {compose_path}")
        for relative in (".env", "wg.env", str(NGINX_CONF_RELATIVE_PATH)):
            target = self.context.project_dir / relative
            console.print(f"[axion.info][dry-run][/] escribiría {target}")
        console.print(
            f"[axion.info][dry-run][/] copiaría el puente FastAPI a "
            f"{self.context.project_dir / 'fastapi'}"
        )
        console.print("[axion.info][dry-run][/] validaría con `docker compose config --quiet`")
