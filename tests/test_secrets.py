import pytest

from axion_wizard.utils import secrets as sec


def test_generate_hex_secret_is_hex_and_correct_length() -> None:
    value = sec.generate_hex_secret(32)
    assert len(value) == 64
    int(value, 16)  # no debe lanzar ValueError


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
    with pytest.raises(sec.WeakPasswordError, match="expansión de variable"):
        sec.validate_wireguard_password("has$dollar")


def test_validate_wireguard_password_accepts_safe_password() -> None:
    sec.validate_wireguard_password("correct-horse-battery-staple-42")  # no debe lanzar


@pytest.mark.parametrize("char", ["$", "`", "!"])
def test_validate_env_value_rejects_forbidden_chars(char: str) -> None:
    with pytest.raises(sec.InvalidEnvValueError) as exc_info:
        sec.validate_env_value(f"token{char}rest", label="el token")
    assert exc_info.value.char == char


def test_validate_env_value_uses_the_given_label() -> None:
    with pytest.raises(sec.InvalidEnvValueError, match="^el token no puede contener"):
        sec.validate_env_value("has$dollar", label="el token")


def test_validate_env_value_accepts_a_safe_value() -> None:
    sec.validate_env_value("token-de-ejemplo-no-real-000", label="el token")  # no debe lanzar


def test_weak_password_error_is_an_invalid_env_value_error() -> None:
    """`WeakPasswordError` es un caso particular de `InvalidEnvValueError`:
    quien capture el genérico también atrapa el de WireGuard."""
    assert issubclass(sec.WeakPasswordError, sec.InvalidEnvValueError)


# --- credenciales del panel: longitud minima (wg-easy v15) ------------------
#
# El wizard ya no hashea nada: la v15 recibe la contrasena en claro y la
# hashea ella. Lo que si valida aqui es la longitud, porque `INIT_PASSWORD`
# no la valida: una contrasena corta crea la cuenta igual y solo falla
# despues, al entrar, con un 400 que parece "contrasena incorrecta".


def test_password_below_the_minimum_is_rejected() -> None:
    with pytest.raises(sec.ShortCredentialError):
        sec.validate_wireguard_password("a" * (sec.MIN_PANEL_PASSWORD_LENGTH - 1))


def test_password_at_the_minimum_is_accepted() -> None:
    sec.validate_wireguard_password("a" * sec.MIN_PANEL_PASSWORD_LENGTH)  # no debe lanzar


def test_forbidden_chars_are_checked_before_length() -> None:
    """Una contrasena larga con un caracter prohibido sigue fallando por el
    caracter, que es el motivo accionable."""
    with pytest.raises(sec.WeakPasswordError):
        sec.validate_wireguard_password("bad`password-pero-larga")


def test_username_below_the_minimum_is_rejected() -> None:
    with pytest.raises(sec.ShortCredentialError):
        sec.validate_wireguard_username("a" * (sec.MIN_PANEL_USERNAME_LENGTH - 1))


def test_username_at_the_minimum_is_accepted() -> None:
    sec.validate_wireguard_username("a" * sec.MIN_PANEL_USERNAME_LENGTH)  # no debe lanzar


def test_short_credential_error_names_the_minimum() -> None:
    """El mensaje llega al prompt tal cual; sin el numero no es accionable."""
    with pytest.raises(sec.ShortCredentialError, match=str(sec.MIN_PANEL_PASSWORD_LENGTH)):
        sec.validate_wireguard_password("corta")


def test_mask_secret_never_leaks_original_value() -> None:
    assert sec.mask_secret("super-secret-token") == "****"
    assert "super-secret-token" not in sec.mask_secret("super-secret-token")


def test_mask_secret_empty_string() -> None:
    assert sec.mask_secret("") == ""


# --- redacción de texto arbitrario (§9) -------------------------------------


@pytest.fixture(autouse=True)
def _isolate_secret_registry():
    """El registro es global; aislarlo evita que un test contamine a otro."""
    sec.clear_registered_secrets()
    yield
    sec.clear_registered_secrets()


def test_redact_masks_a_registered_secret() -> None:
    secret = sec.generate_hex_secret(16)
    sec.register_secret(secret)
    assert secret not in sec.redact(f"algo salió mal con {secret} aquí")
    assert sec.MASK in sec.redact(f"algo salió mal con {secret} aquí")


def test_redact_masks_dsn_password_even_if_not_registered() -> None:
    """El patrón de DSN cubre contraseñas que el wizard nunca generó (p.ej.
    un .env escrito a mano), que el registro por sí solo no conocería."""
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
    assert sec.redact("todo bien por aquí") == "todo bien por aquí"


def test_register_secret_ignores_short_values() -> None:
    """Redactar cadenas cortas destrozaría texto legítimo sin ganar nada."""
    sec.register_secret("abc")
    assert sec.redact("abc def abc") == "abc def abc"


def test_register_secret_ignores_empty_value() -> None:
    sec.register_secret("")
    assert sec.redact("texto normal") == "texto normal"


def test_redact_masks_longest_secret_first() -> None:
    """Si un secreto contiene a otro, enmascarar primero el largo evita
    dejar fragmentos reconocibles del que lo contenía."""
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
