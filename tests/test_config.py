from pathlib import Path

import pytest
from pydantic import ValidationError

from axion_wizard.config import (
    AccessMode,
    AxionConfig,
    WireguardVariant,
    describe_forbidden_wireguard_password_chars,
)
from axion_wizard.utils.secrets import generate_hex_secret, hash_password


def _valid_kwargs(**overrides) -> dict:
    kwargs = dict(
        access_mode=AccessMode.LAN,
        host="192.168.1.50",
        wireguard_variant=WireguardVariant.PORTS,
        postgres_password=generate_hex_secret(),
        wireguard_admin_password_hash=hash_password("correct-horse-battery-staple"),
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


def test_wireguard_hash_rejects_plaintext() -> None:
    with pytest.raises(ValidationError, match="bcrypt"):
        AxionConfig(**_valid_kwargs(wireguard_admin_password_hash="not-a-hash"))


def test_wireguard_hash_accepts_bcrypt() -> None:
    hashed = hash_password("correct-horse-battery-staple-42")
    config = AxionConfig(**_valid_kwargs(wireguard_admin_password_hash=hashed))
    assert config.wireguard_admin_password_hash.get_secret_value().startswith("$2")


def test_secrets_masked_in_repr() -> None:
    config = AxionConfig(**_valid_kwargs())
    rendered = repr(config)
    assert config.postgres_password.get_secret_value() not in rendered
    assert config.wireguard_admin_password_hash.get_secret_value() not in rendered


def test_describe_forbidden_wireguard_password_chars_matches_secrets_module() -> None:
    reasons = describe_forbidden_wireguard_password_chars()
    assert set(reasons) == {"$", "`", "!"}
    assert all(isinstance(v, str) and v for v in reasons.values())
