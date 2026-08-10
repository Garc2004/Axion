"""Modelo Pydantic de la configuración completa del wizard (§4.3).

Los secretos (`postgres_password`, `wireguard_admin_password`) se guardan
como `SecretStr` para que Pydantic los enmascare en `repr()`/`str()` por
defecto — nunca deben aparecer en consola ni en logs (§9). Las tags de
imagen (§6.4) están deliberadamente fuera de este modelo: no son
configurables por el usuario, viven en `axion_wizard.domain.images`.

La contraseña del panel viaja aquí **en claro**, no como hash. No es un
descuido: wg-easy v15 la quiere así en `INIT_PASSWORD` y la hashea ella al
arrancar. Tenerla en claro además arregla algo que con la v14 no tenía
arreglo — el paso 8 podía guardar el hash pero no volver a deducir la
contraseña, así que tenía que pedírsela otra vez al usuario para dar de alta
el primer cliente.
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
    """Configuración completa necesaria para renderizar y desplegar el stack.

    Se construye una sola vez, al final del paso 3, después de que el
    usuario confirma el resumen — de ahí que sea inmutable (`frozen=True`).
    """

    model_config = ConfigDict(frozen=True)

    access_mode: AccessMode
    host: str = Field(..., description="IP LAN o dominio de acceso, según access_mode.")
    wireguard_variant: WireguardVariant
    postgres_password: SecretStr
    wireguard_admin_username: str = Field(
        ..., description="Usuario administrador del panel wg-easy (v15 lo exige)."
    )
    wireguard_admin_password: SecretStr
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

    @field_validator("wireguard_admin_password")
    @classmethod
    def _wireguard_password_is_usable(cls, value: SecretStr) -> SecretStr:
        """Las mismas reglas que el prompt del paso 3, aplicadas otra vez aquí.

        El prompt es el sitio amable para fallar, pero no es el único camino
        hasta este modelo: `--unattended` lo construye desde un `axion.toml`
        y la TUI desde su formulario. Validar en el modelo es lo que garantiza
        que ninguno de los tres pueda escribir un `INIT_PASSWORD` que wg-easy
        vaya a aceptar al crear la cuenta y a rechazar al hacer login.
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
        """Da de alta los secretos de esta configuración para que `redact()`
        los enmascare en cualquier salida (§9), incluida la que el wizard no
        genera —logs de contenedores, stderr de Docker— donde aparecen
        incrustados en medio de otro texto."""
        register_secret(self.postgres_password.get_secret_value())
        register_secret(self.wireguard_admin_password.get_secret_value())
        return self


def describe_forbidden_wireguard_password_chars() -> dict[str, str]:
    """Reexporta los motivos de rechazo de `utils.secrets` para mostrarlos en
    el prompt interactivo del paso 3 sin duplicar la tabla."""
    return dict(FORBIDDEN_PASSWORD_CHAR_REASONS)
