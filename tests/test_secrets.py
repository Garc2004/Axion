import pytest

from axion_wizard.utils import secrets as sec


def test_generate_hex_secret_is_hex_and_correct_length() -> None:
    value = sec.generate_hex_secret(32)
    assert len(value) == 64
    int(value, 16)  # must not raise ValueError


def test_generate_hex_secret_never_contains_url_breaking_chars() -> None:
    for _ in range(50):
        value = sec.generate_hex_secret(32)
        assert not any(c in value for c in "/+=")


def test_generate_hex_secret_is_unique_per_call() -> None:
    assert sec.generate_hex_secret() != sec.generate_hex_secret()


@pytest.mark.parametrize("char", ["$", "`", "!"])
def test_validate_wireguard_password_rejects_forbidden_chars(char: str) -> None:
    with pytest.raises(sec.WeakPasswordError) as exc_info:
        sec.validate_wireguard_password(f"correct-horse{char}battery")
    assert exc_info.value.char == char


def test_validate_wireguard_password_explains_why() -> None:
    with pytest.raises(sec.WeakPasswordError, match="variable expansion"):
        sec.validate_wireguard_password("has$dollar")


def test_validate_wireguard_password_accepts_safe_password() -> None:
    sec.validate_wireguard_password("correct-horse-battery-staple-42")  # must not raise


@pytest.mark.parametrize("char", ["$", "`", "!"])
def test_validate_env_value_rejects_forbidden_chars(char: str) -> None:
    with pytest.raises(sec.InvalidEnvValueError) as exc_info:
        sec.validate_env_value(f"token{char}rest", label="the token")
    assert exc_info.value.char == char


def test_validate_env_value_uses_the_given_label() -> None:
    with pytest.raises(sec.InvalidEnvValueError, match="^the token cannot contain"):
        sec.validate_env_value("has$dollar", label="the token")


def test_validate_env_value_accepts_a_safe_value() -> None:
    sec.validate_env_value("example-token-not-real-000", label="the token")  # must not raise


def test_weak_password_error_is_an_invalid_env_value_error() -> None:
    """`WeakPasswordError` is a special case of `InvalidEnvValueError`: anyone
    catching the generic one also catches WireGuard's."""
    assert issubclass(sec.WeakPasswordError, sec.InvalidEnvValueError)


# --- panel credentials: minimum length (wg-easy v15) ------------------------
#
# The wizard no longer hashes anything: v15 receives the password in the clear
# and hashes it itself. What is validated here is the length, because
# `INIT_PASSWORD` does not: a short password creates the account anyway and
# only fails later, on login, with a 400 that looks like "wrong password".


def test_password_below_the_minimum_is_rejected() -> None:
    with pytest.raises(sec.ShortCredentialError):
        sec.validate_wireguard_password("a" * (sec.MIN_PANEL_PASSWORD_LENGTH - 1))


def test_password_at_the_minimum_is_accepted() -> None:
    sec.validate_wireguard_password("a" * sec.MIN_PANEL_PASSWORD_LENGTH)  # must not raise


def test_forbidden_chars_are_checked_before_length() -> None:
    """A long password with a forbidden character still fails on the
    character, which is the actionable reason."""
    with pytest.raises(sec.WeakPasswordError):
        sec.validate_wireguard_password("bad`password-but-long-enough")


def test_username_below_the_minimum_is_rejected() -> None:
    with pytest.raises(sec.ShortCredentialError):
        sec.validate_wireguard_username("a" * (sec.MIN_PANEL_USERNAME_LENGTH - 1))


def test_username_at_the_minimum_is_accepted() -> None:
    sec.validate_wireguard_username("a" * sec.MIN_PANEL_USERNAME_LENGTH)  # must not raise


def test_short_credential_error_names_the_minimum() -> None:
    """The message reaches the prompt verbatim; without the number it is not actionable."""
    with pytest.raises(sec.ShortCredentialError, match=str(sec.MIN_PANEL_PASSWORD_LENGTH)):
        sec.validate_wireguard_password("corta")


def test_mask_secret_never_leaks_original_value() -> None:
    assert sec.mask_secret("super-secret-token") == "****"
    assert "super-secret-token" not in sec.mask_secret("super-secret-token")


def test_mask_secret_empty_string() -> None:
    assert sec.mask_secret("") == ""


# --- redacting arbitrary text (§9) ------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_secret_registry():
    """The registry is global; isolating it stops one test contaminating another."""
    sec.clear_registered_secrets()
    yield
    sec.clear_registered_secrets()


def test_redact_masks_a_registered_secret() -> None:
    secret = sec.generate_hex_secret(16)
    sec.register_secret(secret)
    assert secret not in sec.redact(f"something went wrong with {secret} here")
    assert sec.MASK in sec.redact(f"something went wrong with {secret} here")


def test_redact_masks_dsn_password_even_if_not_registered() -> None:
    """The DSN pattern covers passwords the wizard never generated (a
    hand-written .env, say), which the registry alone could not know."""
    text = "failed: postgres://mattermost:sup3rs3cr3tvalue@postgres:5432/mattermost?sslmode=disable"
    redacted = sec.redact(text)
    assert "sup3rs3cr3tvalue" not in redacted
    assert "postgres://mattermost:" in redacted
    assert "@postgres:5432" in redacted


def test_redact_preserves_surrounding_text() -> None:
    text = "conectando a postgres://user:hunter2pass@db:5432/x y luego siguiendo"
    redacted = sec.redact(text)
    assert redacted.startswith("conectando a ")
    assert redacted.endswith(" y luego siguiendo")


def test_redact_handles_multiple_dsns() -> None:
    text = "a postgres://u:aaaaaaaaaa@h/1 b redis://u:bbbbbbbbbb@h/2"
    redacted = sec.redact(text)
    assert "aaaaaaaaaa" not in redacted
    assert "bbbbbbbbbb" not in redacted


def test_redact_does_not_touch_urls_without_credentials() -> None:
    text = "GET https://axion.example.com:443/api/v4/system/ping"
    assert sec.redact(text) == text


def test_redact_empty_string() -> None:
    assert sec.redact("") == ""


def test_redact_noop_when_nothing_registered_and_no_dsn() -> None:
    assert sec.redact("all fine over here") == "all fine over here"


def test_register_secret_ignores_short_values() -> None:
    """Redacting short strings would mangle legitimate text for no gain."""
    sec.register_secret("abc")
    assert sec.redact("abc def abc") == "abc def abc"


def test_register_secret_ignores_empty_value() -> None:
    sec.register_secret("")
    assert sec.redact("ordinary text") == "ordinary text"


def test_redact_masks_longest_secret_first() -> None:
    """If one secret contains another, masking the long one first avoids
    leaving recognisable fragments of the one that contained it."""
    short = "abcdefgh12"
    long = short + "34567890"
    sec.register_secret(short)
    sec.register_secret(long)
    redacted = sec.redact(f"valor={long}")
    assert short not in redacted
    assert long not in redacted


def test_clear_registered_secrets() -> None:
    secret = sec.generate_hex_secret(16)
    sec.register_secret(secret)
    sec.clear_registered_secrets()
    assert secret in sec.redact(f"x {secret} y")
