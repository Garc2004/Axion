"""Modelo Pydantic de la configuración completa del wizard (§4.3).

Los secretos (`postgres_password`, `wireguard_admin_password_hash`) se
guardan como `SecretStr` para que Pydantic los enmascare en `repr()`/`str()`
por defecto — nunca deben aparecer en consola ni en logs (§9). Las tags de
imagen (§6.4) están deliberadamente fuera de este modelo: no son
configurables por el usuario, viven en `axion_wizard.domain.images`.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from axion_wizard.detect.platform import WIREGUARD_VARIANT_HOST, WIREGUARD_VARIANT_PORTS
from axion_wizard.utils.secrets import FORBIDDEN_PASSWORD_CHAR_REASONS, register_secret


class AccessMode(StrEnum):
    LAN = "lan"
    DOMAIN = "domain"


class WireguardVariant(StrEnum):
    HOST = WIREGUARD_VARIANT_HOST
    PORTS = WIREGUARD_VARIANT_PORTS


class AxionConfig(BaseModel):
    """Configuración completa necesaria para renderizar y desplegar el stack.

    Se construye una sola vez, al final del paso 3, después de que el
    usuario confirma el resumen — de ahí que sea inmutable (`frozen=True`).
    """

    model_config = ConfigDict(frozen=True)

    access_mode: AccessMode
    host: str = Field(..., description="IP LAN o dominio de acceso, según access_mode.")
    wireguard_variant: WireguardVariant
    postgres_password: SecretStr
    wireguard_admin_password_hash: SecretStr
    ollama_model: str
    project_dir: Path

    @field_validator("host")
    @classmethod
    def _host_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("host no puede estar vacío")
        return value

    @field_validator("ollama_model")
    @classmethod
    def _ollama_model_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("ollama_model no puede estar vacío")
        return value

    @field_validator("postgres_password")
    @classmethod
    def _postgres_password_url_safe(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        breaking_chars = set(raw) & set("/+=")
        if breaking_chars:
            raise ValueError(
                f"postgres_password contiene {sorted(breaking_chars)!r}, que rompe la URL "
                "postgres://user:pass@host:port/db — usar secrets.token_hex, no base64"
            )
        return value

    @field_validator("wireguard_admin_password_hash")
    @classmethod
    def _wireguard_hash_looks_like_bcrypt(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not raw.startswith(("$2a$", "$2b$", "$2y$")):
            raise ValueError(
                "wireguard_admin_password_hash no parece un hash bcrypt "
                "($2a$/$2b$/$2y$) — ¿se guardó la contraseña en claro por error?"
            )
        return value

    @model_validator(mode="after")
    def _register_secrets_for_redaction(self) -> AxionConfig:
        """Da de alta los secretos de esta configuración para que `redact()`
        los enmascare en cualquier salida (§9), incluida la que el wizard no
        genera —logs de contenedores, stderr de Docker— donde aparecen
        incrustados en medio de otro texto."""
        register_secret(self.postgres_password.get_secret_value())
        register_secret(self.wireguard_admin_password_hash.get_secret_value())
        return self


def describe_forbidden_wireguard_password_chars() -> dict[str, str]:
    """Reexporta los motivos de rechazo de `utils.secrets` para mostrarlos en
    el prompt interactivo del paso 3 sin duplicar la tabla."""
    return dict(FORBIDDEN_PASSWORD_CHAR_REASONS)
