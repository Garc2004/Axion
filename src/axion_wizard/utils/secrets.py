"""Generación de secretos y validación de contraseñas (§4.3, §9).

Reglas no negociables de la spec:
- Contraseñas generadas con `secrets`, nunca con `random`.
- La contraseña de PostgreSQL es hex, nunca base64: `/`, `+` y `=` rompen la
  URL de conexión `postgres://user:pass@host:port/db`.
- La contraseña del panel WireGuard rechaza `$`, backtick y `!` antes de
  aceptarse.
- Los secretos nunca se imprimen en consola ni en logs, ni siquiera con
  `--verbose`: usar `mask_secret()` en cualquier punto de salida.

Aquí ya no se hashea nada. wg-easy v14 quería un hash bcrypt en
`PASSWORD_HASH`; la v15 quiere la contraseña en claro en `INIT_PASSWORD` y la
hashea ella al arrancar. El `$` sigue prohibido por el mismo motivo de
siempre —Docker Compose interpola los valores de `env_file:`— pero ahora
protege a la contraseña misma en vez de a su hash.
"""

from __future__ import annotations

import re
import secrets

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


class ShortCredentialError(ValueError):
    """Una credencial del panel es más corta de lo que wg-easy v15 acepta."""

    def __init__(self, label: str, minimum: int, actual: int) -> None:
        self.minimum = minimum
        self.actual = actual
        super().__init__(
            f"{label} necesita al menos {minimum} caracteres (tiene {actual}): "
            "wg-easy v15 rechaza el formulario de login por debajo de ese "
            "mínimo, así que la cuenta quedaría creada y sin poder entrar"
        )


#: Mínimos que wg-easy v15 impone con zod sobre `UserLoginSchema`. No son
#: una política nuestra: son los que valida el propio panel al recibir el
#: login. Si `INIT_PASSWORD` queda por debajo, el contenedor crea la cuenta
#: igual —`INIT_*` no valida longitud— pero después ningún login pasa la
#: validación, y el panel devuelve un 400 que desde fuera parece
#: "contraseña incorrecta". Se comprueba aquí, en el prompt, para que el
#: fallo llegue mientras aún se puede corregir.
MIN_PANEL_PASSWORD_LENGTH = 12
MIN_PANEL_USERNAME_LENGTH = 2


def generate_hex_secret(nbytes: int = 32) -> str:
    """Secreto hexadecimal seguro para URLs de conexión y variables `.env`.

    Usado para la contraseña de PostgreSQL en vez de base64: el alfabeto hex
    (`0-9a-f`) nunca necesita escaparse en una URL `postgres://`.
    """
    return secrets.token_hex(nbytes)


def validate_wireguard_password(password: str) -> None:
    """Valida la contraseña del panel: caracteres y longitud mínima.

    Los caracteres prohibidos protegen la interpolación del `.env`; la
    longitud es la que wg-easy v15 exige para dejar entrar (ver
    `MIN_PANEL_PASSWORD_LENGTH`). No se valida complejidad: eso ya no lo
    mira nadie más abajo, y una regla que solo aplica el wizard no protege
    de nada que el panel no acepte igual.
    """
    for char in password:
        if char in FORBIDDEN_PASSWORD_CHAR_REASONS:
            raise WeakPasswordError(char)
    if len(password) < MIN_PANEL_PASSWORD_LENGTH:
        raise ShortCredentialError(
            "la contraseña del panel", MIN_PANEL_PASSWORD_LENGTH, len(password)
        )


def validate_wireguard_username(username: str) -> None:
    """Valida el usuario del panel, que la v15 introdujo y la v14 no tenía."""
    for char in username:
        if char in FORBIDDEN_PASSWORD_CHAR_REASONS:
            raise InvalidEnvValueError(char, label="el usuario del panel")
    if len(username) < MIN_PANEL_USERNAME_LENGTH:
        raise ShortCredentialError(
            "el usuario del panel", MIN_PANEL_USERNAME_LENGTH, len(username)
        )


def validate_env_value(value: str, *, label: str = "el valor") -> None:
    """Igual que `validate_wireguard_password` pero para cualquier otro valor
    que vaya a parar a un `.env` — p.ej. el token del webhook saliente de
    Mattermost (`set-webhook-token`), que igual de bien podría contener un
    `$` o una comilla invertida y romper la interpolación."""
    for char in value:
        if char in FORBIDDEN_PASSWORD_CHAR_REASONS:
            raise InvalidEnvValueError(char, label=label)


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
