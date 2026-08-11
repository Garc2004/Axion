from pathlib import Path

import pytest
from pydantic import ValidationError

from axion_wizard.domain.config import (
    AccessMode,
    AxionConfig,
    WireguardVariant,
    describe_forbidden_wireguard_password_chars,
)
from axion_wizard.utils.secrets import (
    MIN_PANEL_PASSWORD_LENGTH,
    MIN_PANEL_USERNAME_LENGTH,
    generate_hex_secret,
)


def _valid_kwargs(**overrides) -> dict:
    kwargs = dict(
        access_mode=AccessMode.LAN,
        host="192.168.1.50",
        wireguard_variant=WireguardVariant.PORTS,
        postgres_password=generate_hex_secret(),
        wireguard_admin_username="admin",
        wireguard_admin_password="correct-horse-battery-staple",
        ollama_model="qwen2.5:1.5b",
        project_dir=Path("/tmp/axion"),
    )
    kwargs.update(overrides)
    return kwargs


def test_valid_config_constructs() -> None:
    config = AxionConfig(**_valid_kwargs())
    assert config.access_mode == AccessMode.LAN
    assert config.wireguard_variant == WireguardVariant.PORTS
    assert config.host == "192.168.1.50"


def test_config_is_frozen() -> None:
    config = AxionConfig(**_valid_kwargs())
    with pytest.raises(ValidationError):
        config.host = "10.0.0.1"


def test_host_rejects_empty() -> None:
    with pytest.raises(ValidationError, match="host"):
        AxionConfig(**_valid_kwargs(host="   "))


def test_ollama_model_rejects_empty() -> None:
    with pytest.raises(ValidationError, match="ollama_model"):
        AxionConfig(**_valid_kwargs(ollama_model=""))


@pytest.mark.parametrize("bad_char", ["/", "+", "="])
def test_postgres_password_rejects_base64_breaking_chars(bad_char: str) -> None:
    with pytest.raises(ValidationError, match="postgres_password"):
        AxionConfig(**_valid_kwargs(postgres_password=f"abc{bad_char}def"))


def test_postgres_password_accepts_hex() -> None:
    config = AxionConfig(**_valid_kwargs(postgres_password=generate_hex_secret()))
    assert config.postgres_password.get_secret_value()


# --- panel credentials: the rules are wg-easy's, not ours ------------------------
#
# The model revalidates them even though the prompt already does, because the
# prompt is not the only route here: `--unattended` builds from an
# `axion.toml` and the TUI from its form. A short password does not fail when
# the account is created — `INIT_PASSWORD` does not validate length — but
# later, on login.


@pytest.mark.parametrize("bad_char", ["$", "`", "!"])
def test_panel_password_rejects_chars_that_break_env_interpolation(bad_char: str) -> None:
    with pytest.raises(ValidationError):
        AxionConfig(
            **_valid_kwargs(wireguard_admin_password=f"long-enough-by-far{bad_char}yes")
        )


def test_panel_password_rejects_one_below_the_minimum() -> None:
    short = "a" * (MIN_PANEL_PASSWORD_LENGTH - 1)
    with pytest.raises(ValidationError, match=str(MIN_PANEL_PASSWORD_LENGTH)):
        AxionConfig(**_valid_kwargs(wireguard_admin_password=short))


def test_panel_password_accepts_exactly_the_minimum() -> None:
    exact = "a" * MIN_PANEL_PASSWORD_LENGTH
    config = AxionConfig(**_valid_kwargs(wireguard_admin_password=exact))
    assert config.wireguard_admin_password.get_secret_value() == exact


def test_panel_username_rejects_one_below_the_minimum() -> None:
    short = "a" * (MIN_PANEL_USERNAME_LENGTH - 1)
    with pytest.raises(ValidationError, match=str(MIN_PANEL_USERNAME_LENGTH)):
        AxionConfig(**_valid_kwargs(wireguard_admin_username=short))


def test_panel_username_is_stripped() -> None:
    config = AxionConfig(**_valid_kwargs(wireguard_admin_username="  admin  "))
    assert config.wireguard_admin_username == "admin"


def test_secrets_masked_in_repr() -> None:
    config = AxionConfig(**_valid_kwargs())
    rendered = repr(config)
    assert config.postgres_password.get_secret_value() not in rendered
    assert config.wireguard_admin_password.get_secret_value() not in rendered


def test_describe_forbidden_wireguard_password_chars_matches_secrets_module() -> None:
    reasons = describe_forbidden_wireguard_password_chars()
    assert set(reasons) == {"$", "`", "!"}
    assert all(isinstance(v, str) and v for v in reasons.values())
