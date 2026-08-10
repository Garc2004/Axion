"""Paso 3 — Configuración interactiva (§4.3).

Reúne todo lo que el despliegue necesita y lo consolida en un `AxionConfig`
inmutable. Es el único paso que pregunta, y termina con **una sola**
confirmación sobre un resumen: hasta ese momento no se ha escrito nada al
disco, así que cancelar aquí no deja nada a medias.

Dos reglas de la spec se aplican aquí y no en otro sitio:

- La contraseña de PostgreSQL se genera con `secrets.token_hex`, nunca en
  base64: `/`, `+` y `=` rompen la URL `postgres://user:pass@host:port/db`.
- La del panel de WireGuard se valida *antes* de hashearla, rechazando `$`,
  backtick y `!` con el motivo escrito en el propio prompt.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import SecretStr
from rich.panel import Panel
from rich.table import Table

from axion_wizard.domain.config import AccessMode, AxionConfig, WireguardVariant
from axion_wizard.errors import ConfigError
from axion_wizard.render import ui
from axion_wizard.render.console import console
from axion_wizard.services import ollama
from axion_wizard.steps.base import Step, StepResult
from axion_wizard.steps.prompts import require_interactive_input
from axion_wizard.utils import secrets as secret_utils

ACCESS_MODE_CHOICES = {
    "IP de la LAN (certificado autofirmado)": AccessMode.LAN,
    "Dominio propio (Let's Encrypt DNS-01)": AccessMode.DOMAIN,
}

#: Longitud mínima que se le exige a la contraseña del panel. La spec no fija
#: una; esta es la del propio wg-easy y evita que el panel quede abierto de
#: hecho con una contraseña de tres letras.
MIN_PANEL_PASSWORD_LENGTH = 8


class ConfigStep(Step):
    name = "config"
    title = "Configuración"

    def run(self) -> StepResult:
        if self.state.unattended:
            config = self._build_from_file()
        else:
            config = self._build_interactively()

        self.context.config = config
        return StepResult(
            name=self.name,
            ok=True,
            # Nunca los secretos (§9): solo lo que se puede enseñar.
            data={"host": config.host, "ollama_model": config.ollama_model},
            message=f"acceso {config.access_mode.value} en {config.host}",
        )

    def verify(self) -> StepResult:
        config = self.context.require_config()
        if not config.host:
            return StepResult(name=self.name, ok=False, message="host vacío")
        return StepResult(name=self.name, ok=True, message=f"host {config.host}")

    def restore(self) -> None:
        """Reconstruye la configuración desde `.env` y `wg.env`.

        Es lo que permite reanudar una instalación interrumpida sin volver a
        preguntar nada: los valores ya están escritos en disco desde el paso
        5, incluidos los secretos, así que no hace falta persistirlos aparte
        (y §9 prohíbe hacerlo).
        """
        self.context.config = load_config_from_artifacts(self.context.project_dir)

    # --- camino interactivo --------------------------------------------------------

    def _build_interactively(self) -> AxionConfig:
        import questionary

        # Antes de abrir el primer prompt: sin terminal, `questionary` revienta
        # con `NoConsoleScreenBufferError`, que acababa saliendo como
        # "Error inesperado: No Windows console found" — crudo e inútil.
        require_interactive_input("La configuración interactiva")

        access_mode = self._ask_access_mode(questionary)
        host = self._ask_host(questionary, access_mode)
        panel_password = self._ask_panel_password(questionary)
        model = self._ask_model(questionary)

        config = AxionConfig(
            access_mode=access_mode,
            host=host,
            wireguard_variant=self._variant(),
            postgres_password=SecretStr(
                existing_postgres_password(self.context.project_dir)
                or secret_utils.generate_hex_secret()
            ),
            wireguard_admin_password_hash=SecretStr(secret_utils.hash_password(panel_password)),
            ollama_model=model,
            project_dir=self.context.project_dir,
        )

        console.print(render_summary(config, self.context.warnings))
        if not self.state.yes and not questionary.confirm(
            "¿Aplicar esta configuración?", default=True
        ).ask():
            raise ConfigError(
                what="Configuración no confirmada",
                why="El usuario canceló antes de escribir nada al disco.",
                steps=["Volver a ejecutar `axion-wizard install` cuando quieras."],
            )
        return config

    def _ask_access_mode(self, questionary: Any) -> AccessMode:
        answer = questionary.select(
            "¿Cómo se accederá a AXION?", choices=list(ACCESS_MODE_CHOICES)
        ).ask()
        if answer is None:
            raise _cancelled()
        return ACCESS_MODE_CHOICES[answer]

    def _ask_host(self, questionary: Any, access_mode: AccessMode) -> str:
        suggested = ""
        network = self.context.network
        if access_mode is AccessMode.LAN and network is not None and network.lan_ip:
            suggested = network.lan_ip

        label = (
            "IP de la LAN para acceder a AXION:"
            if access_mode is AccessMode.LAN
            else "Dominio de acceso (p.ej. axion.midominio.com):"
        )
        answer = questionary.text(
            label, default=suggested, validate=_non_empty_validator
        ).ask()
        if answer is None:
            raise _cancelled()
        return answer.strip()

    def _ask_panel_password(self, questionary: Any) -> str:
        console.print(
            "[axion.dim]La contraseña del panel WireGuard no puede contener "
            + ", ".join(f"`{char}`" for char in secret_utils.FORBIDDEN_PASSWORD_CHAR_REASONS)
            + ": rompen la interpretación del shell y de los archivos .env.[/]"
        )
        while True:
            answer = questionary.password(
                "Contraseña del panel WireGuard:", validate=_panel_password_validator
            ).ask()
            if answer is None:
                raise _cancelled()
            repeated = questionary.password("Repite la contraseña:").ask()
            if repeated is None:
                raise _cancelled()
            if answer == repeated:
                return answer
            console.print("[axion.error]Las contraseñas no coinciden.[/]")

    def _ask_model(self, questionary: Any) -> str:
        import asyncio

        hardware = self.context.require_environment().hardware
        catalog = asyncio.run(
            ollama.build_catalog(ram_gb=hardware.ram_total_gb, has_gpu=hardware.has_gpu)
        )
        recommended = ollama.recommended_model(
            catalog, hardware.ram_total_gb, hardware.has_gpu
        )
        choices, default = build_model_choices(catalog, recommended, hardware)
        default = self._current_model_choice(choices) or default

        answer = questionary.select(
            "Modelo de IA a usar:", choices=choices, default=default
        ).ask()
        if answer is None:
            raise _cancelled()
        if answer == ollama.OTHER_MODEL_SENTINEL:
            free = questionary.text(
                "Nombre del modelo en Ollama:", validate=_non_empty_validator
            ).ask()
            if free is None:
                raise _cancelled()
            return free.strip()
        return str(answer)

    def _current_model_choice(self, choices: list[Any]) -> Any | None:
        """La opción correspondiente al modelo que este proyecto ya usa.

        Sin esto, el prompt viene marcado sobre la recomendación del catálogo
        aunque el usuario tenga otro modelo puesto: quien hizo
        `axion-wizard model set qwen2.5:3b` y luego reinstala pierde su
        elección con solo pulsar Enter, y no hay nada que se lo diga. Es el
        mismo problema que ya obligó a conservar la contraseña de PostgreSQL y
        el token del webhook entre ejecuciones.
        """
        from axion_wizard.steps.s05_compose import existing_env_value

        current = existing_env_value(self.context.project_dir, "OLLAMA_MODEL")
        if not current:
            return None
        return next((choice for choice in choices if choice.value == current), None)

    # --- camino desatendido -----------------------------------------------------------

    def _build_from_file(self) -> AxionConfig:
        path = self.state.config_path
        if path is None:
            raise ConfigError(
                what="`--unattended` requiere `--config`",
                why="Sin prompts no hay de dónde sacar host, contraseñas ni modelo.",
                steps=[
                    "Pasar un archivo TOML: axion-wizard install --unattended --config axion.toml",
                    "Ver el formato esperado en el README.",
                ],
            )
        return load_config_from_toml(path, self.context.project_dir, self._variant())

    def _variant(self) -> WireguardVariant:
        return WireguardVariant(self.context.require_environment().wireguard_variant)


# --- carga desde archivo / artefactos --------------------------------------------------


def load_config_from_toml(
    path: Path, project_dir: Path, variant: WireguardVariant
) -> AxionConfig:
    """Lee un `axion.toml` para el modo `--unattended` (§3).

    La contraseña del panel puede venir en claro (`wireguard_admin_password`,
    que se hashea aquí) o ya hasheada (`wireguard_admin_password_hash`), para
    que un archivo de CI no tenga que llevar la contraseña real.
    """
    if not path.exists():
        raise ConfigError(
            what=f"No se encontró el archivo de configuración {path}",
            why="`--unattended` lo necesita para saber qué desplegar.",
            steps=[f"Comprobar la ruta pasada en --config: {path}"],
        )
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ConfigError(
            what=f"No se pudo leer {path}",
            why=str(exc),
            steps=["Revisar la sintaxis TOML del archivo."],
        ) from exc

    password = raw.get("wireguard_admin_password")
    password_hash = raw.get("wireguard_admin_password_hash")
    if not password_hash:
        if not password:
            raise ConfigError(
                what="Falta la contraseña del panel WireGuard en el archivo de configuración",
                why=(
                    "Se necesita `wireguard_admin_password` (en claro, se hashea aquí) "
                    "o `wireguard_admin_password_hash` (bcrypt ya calculado)."
                ),
                steps=["Añadir una de las dos claves al TOML."],
            )
        try:
            password_hash = secret_utils.hash_password(str(password))
        except secret_utils.WeakPasswordError as exc:
            raise ConfigError(
                what="La contraseña del panel WireGuard contiene un carácter prohibido",
                why=str(exc),
                steps=["Elegir otra contraseña sin `$`, backtick ni `!`."],
            ) from exc

    try:
        return AxionConfig(
            access_mode=AccessMode(str(raw.get("access_mode", "lan"))),
            host=str(raw.get("host", "")),
            wireguard_variant=variant,
            postgres_password=SecretStr(
                str(
                    raw.get("postgres_password")
                    or existing_postgres_password(project_dir)
                    or secret_utils.generate_hex_secret()
                )
            ),
            wireguard_admin_password_hash=SecretStr(str(password_hash)),
            ollama_model=str(raw.get("ollama_model", "")),
            project_dir=project_dir,
        )
    except ValueError as exc:
        raise ConfigError(
            what=f"La configuración de {path} no es válida",
            why=str(exc),
            steps=["Corregir los valores señalados y reintentar."],
        ) from exc


def existing_postgres_password(project_dir: Path) -> str | None:
    """Contraseña de PostgreSQL ya escrita en un `.env` de una corrida
    anterior de este mismo `project_dir`, si la hay.

    Postgres solo aplica `POSTGRES_PASSWORD` la *primera* vez que inicializa
    su volumen de datos; en cualquier arranque posterior lo ignora por
    completo. Generar una nueva al azar en cada `install` dejaba a Postgres,
    ya inicializado, con una contraseña vieja que ya no coincidía con la que
    el wizard acababa de escribir en `.env` — Mattermost no lograba
    autenticarse, y nada en la salida del wizard explicaba por qué (incidente
    real, repetido: el `.axion-wizard-state.json` no siempre refleja con
    fiabilidad si el volumen ya se inicializó antes). Leer lo que ya hay en
    disco, si lo hay, es lo único que garantiza coherencia entre corridas.
    """
    from axion_wizard.domain.deployment import env_value

    return env_value(project_dir, "POSTGRES_PASSWORD")


def load_config_from_artifacts(project_dir: Path) -> AxionConfig:
    """Reconstruye `AxionConfig` desde `.env` y `wg.env` ya escritos.

    Lo usa `restore()` al reanudar. El `wireguard_variant` se deduce del
    propio `docker-compose.yml`, igual que hace `doctor`, para no depender de
    que el paso 1 se haya vuelto a ejecutar en esta sesión.
    """
    from axion_wizard.domain.deployment import (
        detect_wireguard_variant_from_compose,
        env_value,
        host_from_site_url,
    )

    host = env_value(project_dir, "WG_HOST", filename="wg.env") or host_from_site_url(
        env_value(project_dir, "MM_SITEURL") or ""
    )
    password = env_value(project_dir, "POSTGRES_PASSWORD")
    password_hash = env_value(project_dir, "PASSWORD_HASH", filename="wg.env")
    model = env_value(project_dir, "OLLAMA_MODEL")

    missing = [
        name
        for name, value in (
            ("WG_HOST/MM_SITEURL", host),
            ("POSTGRES_PASSWORD", password),
            ("PASSWORD_HASH", password_hash),
            ("OLLAMA_MODEL", model),
        )
        if not value
    ]
    if missing or not (host and password and password_hash and model):
        # La segunda mitad de la condición es redundante en tiempo de
        # ejecución, pero es lo que permite al type checker estrechar los
        # `str | None` que devuelve `dotenv_values` para el resto de la
        # función, sin sembrarla de `assert`.
        raise ConfigError(
            what="No se pudo reconstruir la configuración para reanudar",
            why=f"Faltan valores en .env/wg.env: {', '.join(missing)}.",
            steps=[
                "Borrar `.axion-wizard-state.json` para empezar la instalación de cero.",
                "O restaurar los archivos `.env` y `wg.env` del proyecto.",
            ],
        )

    compose_path = project_dir / "docker-compose.yml"
    variant = (
        detect_wireguard_variant_from_compose(compose_path)
        if compose_path.exists()
        else WireguardVariant.PORTS.value
    )

    return AxionConfig(
        access_mode=AccessMode.LAN if _looks_like_ip(host) else AccessMode.DOMAIN,
        host=host,
        wireguard_variant=WireguardVariant(variant),
        postgres_password=SecretStr(password),
        wireguard_admin_password_hash=SecretStr(password_hash),
        ollama_model=model,
        project_dir=project_dir,
    )


def _looks_like_ip(value: str) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


# --- presentación y validadores ----------------------------------------------------------


def render_summary(config: AxionConfig, warnings: list[str] | None = None) -> Panel:
    """Resumen previo a la única confirmación del flujo (§4.3).

    Los secretos salen enmascarados: §9 no admite excepciones ni siquiera en
    la pantalla que el usuario acaba de rellenar.
    """
    table = Table.grid(padding=(0, 2))
    table.add_column(style="axion.label")
    table.add_column(overflow="fold")

    table.add_row("Modo de acceso", config.access_mode.value)
    table.add_row("Host", f"[axion.info]{config.host}[/]")
    table.add_row("Variante WireGuard", config.wireguard_variant.value)
    table.add_row("Modelo de IA", config.ollama_model)
    masked = f"[axion.secret]{secret_utils.MASK}[/]"
    table.add_row("Contraseña PostgreSQL", f"{masked} (generada, 64 hex)")
    table.add_row("Panel WireGuard", f"{masked} (hash bcrypt)")
    table.add_row("Directorio", str(config.project_dir))

    if warnings:
        table.add_row("", "")
        for warning in warnings:
            table.add_row(ui.warn("Aviso"), warning)

    return Panel(
        table,
        title="[axion.heading]Resumen de la configuración[/]",
        title_align="left",
        border_style="axion.border",
        padding=(1, 2),
    )


def _describe_model(model: ollama.ModelInfo, recommended: Any, hardware: Any) -> str:
    marker = (
        f"{ui.GLYPH_OK} " if recommended is not None and model.name == recommended.name else "  "
    )
    size = f"{model.size_gb:.1f} GB" if model.size_bytes else "tamaño desconocido"
    reason = ollama.suitability_reason(model, hardware.ram_total_gb, hardware.has_gpu)
    if model.installed:
        note = "ya instalado"
    else:
        note = reason or "compatible"
    return f"{marker}{model.name} — {size} — {note}"


def build_model_choices(
    catalog: list[ollama.ModelInfo], recommended: ollama.ModelInfo | None, hardware: Any
) -> tuple[list[Any], Any]:
    """Opciones de `questionary` para elegir modelo, y cuál viene marcada.

    Cada opción lleva el **nombre del modelo como valor**, en vez de
    devolver el texto que se pinta y buscarlo después por índice: ese ida y
    vuelta obligaba a que la lista de descripciones y el catálogo siguieran
    alineados posición a posición, una invariante que nada garantizaba.

    Vive aquí, y no en el paso 3, porque `axion-wizard model choose` ofrece
    exactamente la misma lista con el mismo orden y las mismas etiquetas: es
    el catálogo de §5, no un detalle del instalador.
    """
    import questionary

    choices: list[Any] = [
        questionary.Choice(title=_describe_model(model, recommended, hardware), value=model.name)
        for model in catalog
    ]
    # §5: siempre una salida de escape con entrada libre — la librería de
    # Ollama crece constantemente y cualquier lista se queda corta.
    choices.append(
        questionary.Choice(title="Otro (escribir nombre)", value=ollama.OTHER_MODEL_SENTINEL)
    )

    default = None
    if recommended is not None:
        default = next(
            (choice for choice in choices if choice.value == recommended.name), None
        )
    return choices, default


def _non_empty_validator(value: str) -> bool | str:
    return True if value.strip() else "No puede estar vacío."


def _panel_password_validator(value: str) -> bool | str:
    """Valida en vivo, dentro del prompt, para que el motivo se lea junto al
    campo y no como un error después de haberla escrito dos veces."""
    if len(value) < MIN_PANEL_PASSWORD_LENGTH:
        return f"Mínimo {MIN_PANEL_PASSWORD_LENGTH} caracteres."
    try:
        secret_utils.validate_wireguard_password(value)
    except secret_utils.WeakPasswordError as exc:
        return str(exc)
    return True


def _cancelled() -> ConfigError:
    return ConfigError(
        what="Configuración cancelada",
        why="Se interrumpió un prompt antes de terminar; no se escribió nada al disco.",
        steps=["Volver a ejecutar `axion-wizard install` cuando quieras."],
    )
