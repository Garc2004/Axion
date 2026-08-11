"""Step 3 — Interactive configuration (§4.3).

Gathers everything the deployment needs and consolidates it into an immutable
`AxionConfig`. It is the only step that asks anything, and it ends with **one
single** confirmation over a summary: up to that moment nothing has been
written to disk, so cancelling here leaves nothing half-done.

Two of the spec's rules are enforced here and nowhere else:

- The PostgreSQL password is generated with `secrets.token_hex`, never in
  base64: `/`, `+` and `=` break the `postgres://user:pass@host:port/db` URL.
- The WireGuard panel's password rejects `$`, backtick and `!` with the reason
  written into the prompt itself, and enforces the minimum length wg-easy v15
  requires in order to let anyone in. It is no longer hashed: v15 wants it in
  the clear in `INIT_PASSWORD` and hashes it itself (see
  `templates/wg.env.j2`).
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
    "LAN IP (self-signed certificate)": AccessMode.LAN,
    "Own domain (Let's Encrypt DNS-01)": AccessMode.DOMAIN,
}

#: Default panel username, which v15 requires and v14 did not have. It is
#: offered pre-filled rather than imposed: anyone who wants another only has
#: to type over it.
DEFAULT_PANEL_USERNAME = "admin"


class ConfigStep(Step):
    name = "config"
    title = "Configuration"

    def run(self) -> StepResult:
        if self.state.unattended:
            config = self._build_from_file()
        else:
            config = self._build_interactively()

        self.context.config = config
        return StepResult(
            name=self.name,
            ok=True,
            # Never the secrets (§9): only what can be shown.
            data={"host": config.host, "ollama_model": config.ollama_model},
            message=f"{config.access_mode.value} access at {config.host}",
        )

    def verify(self) -> StepResult:
        config = self.context.require_config()
        if not config.host:
            return StepResult(name=self.name, ok=False, message="empty host")
        return StepResult(name=self.name, ok=True, message=f"host {config.host}")

    def restore(self) -> None:
        """Rebuild the configuration from `.env` and `wg.env`.

        This is what lets an interrupted install resume without asking
        anything again: the values have been on disk since step 5, secrets
        included, so there is no need to persist them separately (and §9
        forbids doing so).
        """
        self.context.config = load_config_from_artifacts(self.context.project_dir)

    # --- interactive path ----------------------------------------------------------

    def _build_interactively(self) -> AxionConfig:
        import questionary

        # Before opening the first prompt: with no terminal, `questionary`
        # blows up with `NoConsoleScreenBufferError`, which used to surface as
        # "Unexpected error: No Windows console found" — raw and useless.
        require_interactive_input("Interactive configuration")

        access_mode = self._ask_access_mode(questionary)
        host = self._ask_host(questionary, access_mode)
        panel_username = self._ask_panel_username(questionary)
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
            wireguard_admin_username=panel_username,
            wireguard_admin_password=SecretStr(panel_password),
            ollama_model=model,
            project_dir=self.context.project_dir,
        )

        console.print(render_summary(config, self.context.warnings))
        if not self.state.yes and not questionary.confirm(
            "Apply this configuration?", default=True
        ).ask():
            raise ConfigError(
                what="Configuration not confirmed",
                why="The user cancelled before anything was written to disk.",
                steps=["Run `axion-wizard install` again whenever you like."],
            )
        return config

    def _ask_access_mode(self, questionary: Any) -> AccessMode:
        answer = questionary.select(
            "How will AXION be reached?", choices=list(ACCESS_MODE_CHOICES)
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
            "LAN IP to reach AXION on:"
            if access_mode is AccessMode.LAN
            else "Access domain (e.g. axion.example.com):"
        )
        answer = questionary.text(
            label, default=suggested, validate=_non_empty_validator
        ).ask()
        if answer is None:
            raise _cancelled()
        return answer.strip()

    def _ask_panel_username(self, questionary: Any) -> str:
        answer = questionary.text(
            "WireGuard panel username:",
            default=DEFAULT_PANEL_USERNAME,
            validate=_panel_username_validator,
        ).ask()
        if answer is None:
            raise _cancelled()
        return answer.strip()

    def _ask_panel_password(self, questionary: Any) -> str:
        console.print(
            f"[axion.dim]At least {secret_utils.MIN_PANEL_PASSWORD_LENGTH} characters "
            "(what wg-easy requires in order to let you in), and no "
            + ", ".join(f"`{char}`" for char in secret_utils.FORBIDDEN_PASSWORD_CHAR_REASONS)
            + ": they break shell and .env file interpretation.[/]"
        )
        while True:
            answer = questionary.password(
                "WireGuard panel password:", validate=_panel_password_validator
            ).ask()
            if answer is None:
                raise _cancelled()
            repeated = questionary.password("Repeat the password:").ask()
            if repeated is None:
                raise _cancelled()
            if answer == repeated:
                return answer
            console.print("[axion.error]The passwords do not match.[/]")

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
            "AI model to use:", choices=choices, default=default
        ).ask()
        if answer is None:
            raise _cancelled()
        if answer == ollama.OTHER_MODEL_SENTINEL:
            free = questionary.text(
                "Model name in Ollama:", validate=_non_empty_validator
            ).ask()
            if free is None:
                raise _cancelled()
            return free.strip()
        return str(answer)

    def _current_model_choice(self, choices: list[Any]) -> Any | None:
        """The option matching the model this project already uses.

        Without this, the prompt comes pre-selected on the catalogue's
        recommendation even when the user has another model set: anyone who
        ran `axion-wizard model set qwen2.5:3b` and then reinstalls loses
        their choice by simply pressing Enter, with nothing to tell them. It
        is the same problem that already forced preserving the PostgreSQL
        password and the webhook token across runs.
        """
        from axion_wizard.steps.s05_compose import existing_env_value

        current = existing_env_value(self.context.project_dir, "OLLAMA_MODEL")
        if not current:
            return None
        return next((choice for choice in choices if choice.value == current), None)

    # --- unattended path --------------------------------------------------------------

    def _build_from_file(self) -> AxionConfig:
        path = self.state.config_path
        if path is None:
            raise ConfigError(
                what="`--unattended` requires `--config`",
                why=(
                    "With no prompts there is nowhere to get the host, passwords or "
                    "model from."
                ),
                steps=[
                    "Pass a TOML file: axion-wizard install --unattended --config axion.toml",
                    "See the expected format in the README.",
                ],
            )
        return load_config_from_toml(path, self.context.project_dir, self._variant())

    def _variant(self) -> WireguardVariant:
        return WireguardVariant(self.context.require_environment().wireguard_variant)


# --- loading from file / artifacts -----------------------------------------------------


def load_config_from_toml(
    path: Path, project_dir: Path, variant: WireguardVariant
) -> AxionConfig:
    """Read an `axion.toml` for `--unattended` mode (§3).

    The panel password arrives in the clear (`wireguard_admin_password`). An
    already-computed bcrypt hash used to be accepted as well, so a CI file
    would not have to carry the real password; with wg-easy v15 that
    alternative ceased to exist, because the panel itself does the hashing and
    only accepts `INIT_PASSWORD` in the clear. Anyone who does not want the
    value inside the TOML has to keep that file out of the repository, like
    any other deployment secret.

    The username (`wireguard_admin_username`) is optional: if absent,
    `DEFAULT_PANEL_USERNAME` is used, which is what the interactive path
    offers pre-filled.
    """
    if not path.exists():
        raise ConfigError(
            what=f"Configuration file {path} was not found",
            why="`--unattended` needs it in order to know what to deploy.",
            steps=[f"Check the path given to --config: {path}"],
        )
    try:
        # `utf-8-sig`, not `utf-8`: on Windows the natural ways of creating this
        # file (Notepad's "UTF-8", `Out-File -Encoding utf8` on PowerShell 5.1)
        # all prepend a BOM, and tomllib then rejects the whole file with
        # "Invalid statement (at line 1, column 1)" — which points at a line
        # that is perfectly fine and says nothing about the real cause. The
        # codec strips a BOM if present and decodes plain UTF-8 unchanged.
        raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ConfigError(
            what=f"Could not read {path}",
            why=str(exc),
            steps=["Check the file's TOML syntax."],
        ) from exc

    password = raw.get("wireguard_admin_password")
    if not password:
        if raw.get("wireguard_admin_password_hash"):
            # Its own message rather than "the password is missing": anyone
            # arriving from an earlier version has the key set and needs to
            # know it no longer works, not that they forgot it.
            raise ConfigError(
                what="`wireguard_admin_password_hash` is no longer accepted",
                why=(
                    "It served wg-easy v14, which received a bcrypt hash. v15 hashes "
                    "the password itself and only accepts it in the clear."
                ),
                steps=[
                    "Replace it with `wireguard_admin_password` holding the real password.",
                    "Keep axion.toml out of version control.",
                ],
            )
        raise ConfigError(
            what="The WireGuard panel password is missing from the configuration file",
            why="`wireguard_admin_password` is required in order to configure wg-easy.",
            steps=["Add `wireguard_admin_password` to the TOML."],
        )

    # The credentials are validated here as well as in the model.
    # `AxionConfig` would reject them anyway, but wrapped in a Pydantic
    # `ValidationError` that surfaces as "the configuration in axion.toml is
    # not valid": correct and not much use. This path has nobody in front of
    # it to re-read a prompt, so the reason has to be in the error's title.
    try:
        secret_utils.validate_wireguard_username(
            str(raw.get("wireguard_admin_username") or DEFAULT_PANEL_USERNAME)
        )
        secret_utils.validate_wireguard_password(str(password))
    except secret_utils.InvalidEnvValueError as exc:
        raise ConfigError(
            what="The WireGuard panel credentials contain a forbidden character",
            why=str(exc),
            steps=["Choose another value without `$`, backtick or `!`."],
        ) from exc
    except secret_utils.ShortCredentialError as exc:
        raise ConfigError(
            what="The WireGuard panel credentials are too short",
            why=str(exc),
            steps=[
                f"Use a password of at least {secret_utils.MIN_PANEL_PASSWORD_LENGTH} "
                "characters in `wireguard_admin_password`.",
            ],
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
            wireguard_admin_username=str(
                raw.get("wireguard_admin_username") or DEFAULT_PANEL_USERNAME
            ),
            wireguard_admin_password=SecretStr(str(password)),
            ollama_model=str(raw.get("ollama_model", "")),
            project_dir=project_dir,
        )
    except ValueError as exc:
        raise ConfigError(
            what=f"The configuration in {path} is not valid",
            why=str(exc),
            steps=["Correct the values flagged above and retry."],
        ) from exc


def existing_postgres_password(project_dir: Path) -> str | None:
    """The PostgreSQL password already written into a `.env` from a previous
    run of this same `project_dir`, if there is one.

    Postgres only applies `POSTGRES_PASSWORD` the *first* time it initialises
    its data volume; on any later start it ignores it entirely. Generating a
    fresh random one on every `install` left an already-initialised Postgres
    holding an old password that no longer matched the one the wizard had just
    written into `.env` — Mattermost could not authenticate, and nothing in
    the wizard's output explained why (a real, repeated incident:
    `.axion-wizard-state.json` does not always reliably reflect whether the
    volume was initialised before). Reading what is already on disk, when
    there is something, is the only thing that guarantees consistency across
    runs.
    """
    from axion_wizard.domain.deployment import env_value

    return env_value(project_dir, "POSTGRES_PASSWORD")


def load_config_from_artifacts(project_dir: Path) -> AxionConfig:
    """Rebuild `AxionConfig` from the already-written `.env` and `wg.env`.

    Used by `restore()` when resuming. The `wireguard_variant` is inferred
    from `docker-compose.yml` itself, as `doctor` does, so as not to depend on
    step 1 having run again in this session.
    """
    from axion_wizard.domain.deployment import (
        detect_wireguard_variant_from_compose,
        env_value,
        host_from_site_url,
    )

    host = env_value(project_dir, "INIT_HOST", filename="wg.env") or host_from_site_url(
        env_value(project_dir, "MM_SITEURL") or ""
    )
    password = env_value(project_dir, "POSTGRES_PASSWORD")
    panel_username = env_value(project_dir, "INIT_USERNAME", filename="wg.env")
    panel_password = env_value(project_dir, "INIT_PASSWORD", filename="wg.env")
    model = env_value(project_dir, "OLLAMA_MODEL")

    missing = [
        name
        for name, value in (
            ("INIT_HOST/MM_SITEURL", host),
            ("POSTGRES_PASSWORD", password),
            ("INIT_USERNAME", panel_username),
            ("INIT_PASSWORD", panel_password),
            ("OLLAMA_MODEL", model),
        )
        if not value
    ]
    if missing or not (host and password and panel_username and panel_password and model):
        # The second half of the condition is redundant at runtime, but it is
        # what lets the type checker narrow the `str | None` values for the
        # rest of the function without seeding it with `assert`s.
        raise ConfigError(
            what="The configuration could not be rebuilt in order to resume",
            why=f"Values missing from .env/wg.env: {', '.join(missing)}.",
            steps=[
                "Delete `.axion-wizard-state.json` to start the install from scratch.",
                "Or restore the project's `.env` and `wg.env` files.",
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
        wireguard_admin_username=panel_username,
        wireguard_admin_password=SecretStr(panel_password),
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


# --- presentation and validators ---------------------------------------------------------


def render_summary(config: AxionConfig, warnings: list[str] | None = None) -> Panel:
    """The summary preceding the flow's single confirmation (§4.3).

    Secrets come out masked: §9 admits no exceptions, not even on the screen
    the user has just filled in.
    """
    table = Table.grid(padding=(0, 2))
    table.add_column(style="axion.label")
    table.add_column(overflow="fold")

    table.add_row("Access mode", config.access_mode.value)
    table.add_row("Host", f"[axion.info]{config.host}[/]")
    table.add_row("WireGuard variant", config.wireguard_variant.value)
    table.add_row("AI model", config.ollama_model)
    masked = f"[axion.secret]{secret_utils.MASK}[/]"
    table.add_row("PostgreSQL password", f"{masked} (generated, 64 hex)")
    table.add_row(
        "WireGuard panel", f"{config.wireguard_admin_username} / {masked}"
    )
    table.add_row("Directory", str(config.project_dir))

    if warnings:
        table.add_row("", "")
        for warning in warnings:
            table.add_row(ui.warn("Warning"), warning)

    return Panel(
        table,
        title="[axion.heading]Configuration summary[/]",
        title_align="left",
        border_style="axion.border",
        padding=(1, 2),
    )


def _describe_model(model: ollama.ModelInfo, recommended: Any, hardware: Any) -> str:
    marker = (
        f"{ui.GLYPH_OK} " if recommended is not None and model.name == recommended.name else "  "
    )
    size = f"{model.size_gb:.1f} GB" if model.size_bytes else "size unknown"
    reason = ollama.suitability_reason(model, hardware.ram_total_gb, hardware.has_gpu)
    if model.installed:
        note = "already installed"
    else:
        note = reason or "compatible"
    return f"{marker}{model.name} — {size} — {note}"


def build_model_choices(
    catalog: list[ollama.ModelInfo], recommended: ollama.ModelInfo | None, hardware: Any
) -> tuple[list[Any], Any]:
    """The `questionary` options for choosing a model, and which is selected.

    Each option carries the **model name as its value**, rather than returning
    the text that gets drawn and looking it up afterwards by index: that round
    trip required the description list and the catalogue to stay aligned
    position by position, an invariant nothing guaranteed.

    It lives here, and not in step 3, because `axion-wizard model choose`
    offers exactly the same list in the same order with the same labels: it is
    §5's catalogue, not an installer detail.
    """
    import questionary

    choices: list[Any] = [
        questionary.Choice(title=_describe_model(model, recommended, hardware), value=model.name)
        for model in catalog
    ]
    # §5: always an escape hatch with free-text entry — Ollama's library grows
    # constantly and any list falls short.
    choices.append(
        questionary.Choice(title="Other (type a name)", value=ollama.OTHER_MODEL_SENTINEL)
    )

    default = None
    if recommended is not None:
        default = next(
            (choice for choice in choices if choice.value == recommended.name), None
        )
    return choices, default


def _non_empty_validator(value: str) -> bool | str:
    return True if value.strip() else "This cannot be empty."


def _panel_password_validator(value: str) -> bool | str:
    """Validate live, inside the prompt, so the reason reads next to the field
    rather than as an error after typing it twice.

    The minimum length is not cosmetic: `INIT_PASSWORD` does not validate it,
    so a short password creates the account anyway and only fails *later*, on
    trying to log in, with a 400 that from the outside looks like "wrong
    password". Here it can still be corrected.
    """
    try:
        secret_utils.validate_wireguard_password(value)
    except (secret_utils.WeakPasswordError, secret_utils.ShortCredentialError) as exc:
        return str(exc)
    return True


def _panel_username_validator(value: str) -> bool | str:
    try:
        secret_utils.validate_wireguard_username(value.strip())
    except (secret_utils.InvalidEnvValueError, secret_utils.ShortCredentialError) as exc:
        return str(exc)
    return True


def _cancelled() -> ConfigError:
    return ConfigError(
        what="Configuration cancelled",
        why="A prompt was interrupted before finishing; nothing was written to disk.",
        steps=["Run `axion-wizard install` again whenever you like."],
    )
