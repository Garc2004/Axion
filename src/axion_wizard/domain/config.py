"""Pydantic model of the wizard's complete configuration (§4.3).

Secrets (`postgres_password`, `wireguard_admin_password`) are held as
`SecretStr` so Pydantic masks them in `repr()`/`str()` by default — they must
never reach the console or the logs (§9). Image tags (§6.4) are deliberately
outside this model: they are not user-configurable and live in
`axion_wizard.domain.images`.

The panel password travels here **in the clear**, not as a hash. That is not
an oversight: wg-easy v15 wants it that way in `INIT_PASSWORD` and hashes it
itself on startup. Having it in the clear also fixes something that had no
fix under v14 — step 8 could store the hash but never recover the password
from it, so it had to ask the user for it a second time in order to enrol the
first client.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from axion_wizard.detect.platform import WIREGUARD_VARIANT_HOST, WIREGUARD_VARIANT_PORTS
from axion_wizard.utils.secrets import (
    FORBIDDEN_PASSWORD_CHAR_REASONS,
    register_secret,
    validate_wireguard_password,
    validate_wireguard_username,
)


class AccessMode(StrEnum):
    LAN = "lan"
    DOMAIN = "domain"


class WireguardVariant(StrEnum):
    HOST = WIREGUARD_VARIANT_HOST
    PORTS = WIREGUARD_VARIANT_PORTS


class AxionConfig(BaseModel):
    """Everything needed to render and deploy the stack.

    Built exactly once, at the end of step 3, after the user confirms the
    summary — hence immutable (`frozen=True`).
    """

    model_config = ConfigDict(frozen=True)

    access_mode: AccessMode
    host: str = Field(..., description="LAN IP or access domain, per access_mode.")
    wireguard_variant: WireguardVariant
    postgres_password: SecretStr
    wireguard_admin_username: str = Field(
        ..., description="Admin username for the wg-easy panel (v15 requires one)."
    )
    wireguard_admin_password: SecretStr
    ollama_model: str
    project_dir: Path

    @field_validator("host")
    @classmethod
    def _host_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("host cannot be empty")
        return value

    @field_validator("ollama_model")
    @classmethod
    def _ollama_model_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("ollama_model cannot be empty")
        return value

    @field_validator("postgres_password")
    @classmethod
    def _postgres_password_url_safe(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        breaking_chars = set(raw) & set("/+=")
        if breaking_chars:
            raise ValueError(
                f"postgres_password contains {sorted(breaking_chars)!r}, which breaks the "
                "postgres://user:pass@host:port/db URL — use secrets.token_hex, not base64"
            )
        return value

    @field_validator("wireguard_admin_password")
    @classmethod
    def _wireguard_password_is_usable(cls, value: SecretStr) -> SecretStr:
        """The same rules as step 3's prompt, applied again here.

        The prompt is the kind place to fail, but it is not the only route to
        this model: `--unattended` builds it from an `axion.toml` and the TUI
        from its form. Validating in the model is what guarantees none of the
        three can write an `INIT_PASSWORD` that wg-easy will accept when
        creating the account and reject at login.
        """
        validate_wireguard_password(value.get_secret_value())
        return value

    @field_validator("wireguard_admin_username")
    @classmethod
    def _wireguard_username_is_usable(cls, value: str) -> str:
        value = value.strip()
        validate_wireguard_username(value)
        return value

    @model_validator(mode="after")
    def _register_secrets_for_redaction(self) -> AxionConfig:
        """Register this configuration's secrets so `redact()` masks them in
        any output (§9), including output the wizard did not generate —
        container logs, Docker's stderr — where they appear embedded in the
        middle of other text."""
        register_secret(self.postgres_password.get_secret_value())
        register_secret(self.wireguard_admin_password.get_secret_value())
        return self


def describe_forbidden_wireguard_password_chars() -> dict[str, str]:
    """Re-export the rejection reasons from `utils.secrets` so step 3's
    interactive prompt can show them without duplicating the table."""
    return dict(FORBIDDEN_PASSWORD_CHAR_REASONS)
