"""Generación de secretos y validación de contraseñas (§4.3, §9).

Reglas no negociables de la spec:
- Contraseñas generadas con `secrets`, nunca con `random`.
- La contraseña de PostgreSQL es hex, nunca base64: `/`, `+` y `=` rompen la
  URL de conexión `postgres://user:pass@host:port/db`.
- La contraseña del panel WireGuard rechaza `$`, backtick y `!` antes de
  aceptarse, y se hashea con bcrypt en Python (nunca invocando el contenedor).
- Los secretos nunca se imprimen en consola ni en logs, ni siquiera con
  `--verbose`: usar `mask_secret()` en cualquier punto de salida.
"""

from __future__ import annotations

import re
import secrets

import bcrypt

MASK = "****"

#: longitud mínima de un valor para registrarlo como secreto redactable.
#: Redactar cadenas muy cortas destrozaría texto legítimo sin aportar nada.
MIN_REDACTABLE_LENGTH = 8

FORBIDDEN_PASSWORD_CHAR_REASONS: dict[str, str] = {
    "$": "se interpreta como expansión de variable en shell y en archivos .env",
    "`": "dispara sustitución de comando en shells POSIX",
    "!": "dispara expansión de historial en bash interactivo y puede truncar la contraseña",
}


class InvalidEnvValueError(ValueError):
    """Un valor destinado a un `.env` contiene un carácter prohibido."""

    def __init__(self, char: str, *, label: str = "el valor") -> None:
        self.char = char
        reason = FORBIDDEN_PASSWORD_CHAR_REASONS.get(char, "rompe la interpretación del shell/env")
        super().__init__(f"{label} no puede contener {char!r}: {reason}")


class WeakPasswordError(InvalidEnvValueError):
    """La contraseña contiene un carácter prohibido para el panel WireGuard."""

    def __init__(self, char: str) -> None:
        super().__init__(char, label="la contraseña")


def generate_hex_secret(nbytes: int = 32) -> str:
    """Secreto hexadecimal seguro para URLs de conexión y variables `.env`.

    Usado para la contraseña de PostgreSQL en vez de base64: el alfabeto hex
    (`0-9a-f`) nunca necesita escaparse en una URL `postgres://`.
    """
    return secrets.token_hex(nbytes)


def validate_wireguard_password(password: str) -> None:
    """Lanza `WeakPasswordError` con el motivo si `password` contiene un
    carácter prohibido. No valida longitud ni complejidad — solo lo que
    rompería el panel de wg-easy o el propio shell del wizard."""
    for char in password:
        if char in FORBIDDEN_PASSWORD_CHAR_REASONS:
            raise WeakPasswordError(char)


def validate_env_value(value: str, *, label: str = "el valor") -> None:
    """Igual que `validate_wireguard_password` pero para cualquier otro valor
    que vaya a parar a un `.env` — p.ej. el token del webhook saliente de
    Mattermost (`set-webhook-token`), que igual de bien podría contener un
    `$` o una comilla invertida y romper la interpolación."""
    for char in value:
        if char in FORBIDDEN_PASSWORD_CHAR_REASONS:
            raise InvalidEnvValueError(char, label=label)


def hash_password(password: str) -> str:
    """Hash bcrypt generado en Python, nunca invocando el contenedor de wg-easy.

    Valida contra `validate_wireguard_password` primero: un hash de una
    contraseña inválida sería una falsa sensación de seguridad.
    """
    validate_wireguard_password(password)
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("ascii"))


def mask_secret(value: str) -> str:
    """Máscara para cualquier valor secreto que vaya a consola o log."""
    return MASK if value else ""


# --- Redacción de texto arbitrario (§9) ---------------------------------------
#
# `mask_secret` sirve cuando sabemos que *todo* el valor es un secreto. Pero
# el wizard también muestra texto que no generó él —los últimos 30 renglones
# del log de un contenedor que falló (§4.6), por ejemplo— y ahí el secreto
# viene incrustado: Mattermost y PostgreSQL registran su DSN completo,
# contraseña incluida, cuando no logran conectar. Mostrarlo tal cual
# rompería "ningún secreto aparece en consola ni en el log".

#: `esquema://usuario:contraseña@host`, el formato en que se filtra la
#: contraseña de PostgreSQL a través de los logs de los contenedores.
_DSN_CREDENTIALS_RE = re.compile(
    r"(?P<prefix>[a-zA-Z][a-zA-Z0-9+.\-]*://[^:/?#\s@]+:)(?P<password>[^@\s]+)(?P<suffix>@)"
)

_registered_secrets: set[str] = set()


def register_secret(value: str) -> None:
    """Registra un valor concreto para enmascararlo en cualquier salida.

    Se ignoran los valores muy cortos: redactarlos ensuciaría texto
    legítimo (un "abc" cualquiera) sin ganancia real de seguridad.
    """
    if value and len(value) >= MIN_REDACTABLE_LENGTH:
        _registered_secrets.add(value)


def clear_registered_secrets() -> None:
    """Vacía el registro. Pensado para aislar tests entre sí."""
    _registered_secrets.clear()


def redact(text: str) -> str:
    """Enmascara secretos conocidos y credenciales embebidas en `text`.

    Combina dos estrategias a propósito: los valores registrados atrapan lo
    que el wizard generó, y el patrón de DSN atrapa además contraseñas que
    nunca pasaron por aquí (p.ej. las de un `.env` escrito a mano por el
    usuario), que el registro por sí solo no podría conocer.
    """
    if not text:
        return text

    redacted = text
    # De más largo a más corto: si un secreto contiene a otro, enmascarar
    # primero el largo evita dejar fragmentos del que lo contenía.
    for secret in sorted(_registered_secrets, key=len, reverse=True):
        redacted = redacted.replace(secret, MASK)

    return _DSN_CREDENTIALS_RE.sub(rf"\g<prefix>{MASK}\g<suffix>", redacted)
