"""Secret generation and password validation (§4.3, §9).

Non-negotiable rules from the spec:
- Passwords generated with `secrets`, never with `random`.
- The PostgreSQL password is hex, never base64: `/`, `+` and `=` break the
  connection URL `postgres://user:pass@host:port/db`.
- The WireGuard panel password rejects `$`, backtick and `!` before being
  accepted.
- Secrets never reach the console or the logs, not even under `--verbose`:
  use `mask_secret()` at every output point.

Nothing is hashed here any more. wg-easy v14 wanted a bcrypt hash in
`PASSWORD_HASH`; v15 wants the password in the clear in `INIT_PASSWORD` and
hashes it itself on startup. `$` stays forbidden for the same reason as
always — Docker Compose interpolates the values of `env_file:` — but now it
protects the password itself rather than its hash.
"""

from __future__ import annotations

import re
import secrets

MASK = "****"

#: Minimum length for a value to be registered as a redactable secret.
#: Redacting very short strings would mangle legitimate text for no gain.
MIN_REDACTABLE_LENGTH = 8

FORBIDDEN_PASSWORD_CHAR_REASONS: dict[str, str] = {
    "$": "is read as variable expansion in the shell and in .env files",
    "`": "triggers command substitution in POSIX shells",
    "!": "triggers history expansion in interactive bash and can truncate the password",
}


class InvalidEnvValueError(ValueError):
    """A value headed for a `.env` contains a forbidden character."""

    def __init__(self, char: str, *, label: str = "the value") -> None:
        self.char = char
        reason = FORBIDDEN_PASSWORD_CHAR_REASONS.get(char, "breaks shell/env interpretation")
        super().__init__(f"{label} cannot contain {char!r}: it {reason}")


class WeakPasswordError(InvalidEnvValueError):
    """The password contains a character forbidden for the WireGuard panel."""

    def __init__(self, char: str) -> None:
        super().__init__(char, label="the password")


class ShortCredentialError(ValueError):
    """A panel credential is shorter than wg-easy v15 will accept."""

    def __init__(self, label: str, minimum: int, actual: int) -> None:
        self.minimum = minimum
        self.actual = actual
        super().__init__(
            f"{label} needs at least {minimum} characters (it has {actual}): "
            "wg-easy v15 rejects the login form below that minimum, so the "
            "account would be created and then be impossible to log into"
        )


#: Minimums wg-easy v15 enforces with zod on `UserLoginSchema`. These are not
#: our policy: they are what the panel itself validates when it receives a
#: login. If `INIT_PASSWORD` falls below, the container creates the account
#: anyway — `INIT_*` does not validate length — but afterwards no login
#: passes validation, and the panel returns a 400 that from the outside looks
#: like "wrong password". Checked here, at the prompt, so the failure arrives
#: while it can still be corrected.
MIN_PANEL_PASSWORD_LENGTH = 12
MIN_PANEL_USERNAME_LENGTH = 2


def generate_hex_secret(nbytes: int = 32) -> str:
    """A secure hex secret for connection URLs and `.env` variables.

    Used for the PostgreSQL password instead of base64: the hex alphabet
    (`0-9a-f`) never needs escaping inside a `postgres://` URL.
    """
    return secrets.token_hex(nbytes)


def validate_wireguard_password(password: str) -> None:
    """Validate the panel password: characters and minimum length.

    The forbidden characters protect `.env` interpolation; the length is what
    wg-easy v15 demands in order to let anyone in (see
    `MIN_PANEL_PASSWORD_LENGTH`). Complexity is not validated: nothing further
    down looks at it, and a rule only the wizard enforces protects against
    nothing the panel would not accept anyway.
    """
    for char in password:
        if char in FORBIDDEN_PASSWORD_CHAR_REASONS:
            raise WeakPasswordError(char)
    if len(password) < MIN_PANEL_PASSWORD_LENGTH:
        raise ShortCredentialError(
            "the panel password", MIN_PANEL_PASSWORD_LENGTH, len(password)
        )


def validate_wireguard_username(username: str) -> None:
    """Validate the panel username, which v15 introduced and v14 had not."""
    for char in username:
        if char in FORBIDDEN_PASSWORD_CHAR_REASONS:
            raise InvalidEnvValueError(char, label="the panel username")
    if len(username) < MIN_PANEL_USERNAME_LENGTH:
        raise ShortCredentialError(
            "the panel username", MIN_PANEL_USERNAME_LENGTH, len(username)
        )


def validate_env_value(value: str, *, label: str = "the value") -> None:
    """Like `validate_wireguard_password` but for any other value headed for a
    `.env` — e.g. Mattermost's outgoing webhook token (`set-webhook-token`),
    which could just as easily contain a `$` or a backtick and break
    interpolation."""
    for char in value:
        if char in FORBIDDEN_PASSWORD_CHAR_REASONS:
            raise InvalidEnvValueError(char, label=label)


def mask_secret(value: str) -> str:
    """Mask for any secret value headed for the console or a log."""
    return MASK if value else ""


# --- Redacting arbitrary text (§9) --------------------------------------------
#
# `mask_secret` works when we know the *whole* value is a secret. But the
# wizard also displays text it did not generate — the last 30 lines of a
# failed container's log (§4.6), for instance — and there the secret comes
# embedded: Mattermost and PostgreSQL log their full DSN, password included,
# when they cannot connect. Showing that verbatim would break "no secret ever
# appears in the console or the log".

#: `scheme://user:password@host`, the shape in which the PostgreSQL password
#: leaks through the containers' logs.
_DSN_CREDENTIALS_RE = re.compile(
    r"(?P<prefix>[a-zA-Z][a-zA-Z0-9+.\-]*://[^:/?#\s@]+:)(?P<password>[^@\s]+)(?P<suffix>@)"
)

_registered_secrets: set[str] = set()


def register_secret(value: str) -> None:
    """Register a specific value so it is masked in any output.

    Very short values are ignored: redacting them would dirty legitimate text
    (any stray "abc") for no real security gain.
    """
    if value and len(value) >= MIN_REDACTABLE_LENGTH:
        _registered_secrets.add(value)


def clear_registered_secrets() -> None:
    """Empty the registry. Meant for isolating tests from each other."""
    _registered_secrets.clear()


def redact(text: str) -> str:
    """Mask known secrets and embedded credentials in `text`.

    Two strategies on purpose: the registered values catch what the wizard
    generated, and the DSN pattern additionally catches passwords that never
    passed through here (e.g. those in a hand-written `.env`), which the
    registry alone could not know about.
    """
    if not text:
        return text

    redacted = text
    # Longest to shortest: if one secret contains another, masking the long
    # one first avoids leaving fragments of the one that contained it.
    for secret in sorted(_registered_secrets, key=len, reverse=True):
        redacted = redacted.replace(secret, MASK)

    return _DSN_CREDENTIALS_RE.sub(rf"\g<prefix>{MASK}\g<suffix>", redacted)
