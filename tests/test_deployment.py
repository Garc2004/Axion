"""Tests for `axion_wizard.deployment` — reading a deployment back off disk.

These moved here from `test_verify.py` when the discovery code moved out of
step 9: `doctor`, `wireguard add-client` and step 3's `restore()` all depend
on it, and none of them is the verification step.
"""

from pathlib import Path

import pytest

from axion_wizard.config import WireguardVariant
from axion_wizard.deployment import (
    detect_wireguard_variant_from_compose,
    discover_deployment,
    env_value,
    host_from_site_url,
)
from axion_wizard.errors import ConfigError

# --- discover_deployment ----------------------------------------------------------


def test_discover_deployment_missing_compose_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="docker-compose.yml"):
        discover_deployment(tmp_path)


def _write_minimal_project(tmp_path: Path, wireguard_service: str = "") -> None:
    compose_body = "services:\n  wireguard:\n" + (wireguard_service or "    image: x\n")
    (tmp_path / "docker-compose.yml").write_text(compose_body)
    (tmp_path / ".env").write_text("OLLAMA_MODEL=qwen2.5:1.5b\nMM_SITEURL=https://192.168.1.50\n")
    (tmp_path / "wg.env").write_text("WG_HOST=192.168.1.50\n")


def test_discover_deployment_reads_host_from_wg_env(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    facts = discover_deployment(tmp_path)
    assert facts.host == "192.168.1.50"
    assert facts.ollama_model == "qwen2.5:1.5b"
    assert facts.wireguard_variant == WireguardVariant.PORTS.value


def test_discover_deployment_falls_back_to_site_url_when_no_wg_host(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yml").write_text("services:\n  wireguard:\n    image: x\n")
    (tmp_path / ".env").write_text(
        "OLLAMA_MODEL=qwen2.5:1.5b\nMM_SITEURL=https://axion.example.com\n"
    )
    facts = discover_deployment(tmp_path)
    assert facts.host == "axion.example.com"


@pytest.mark.parametrize(
    ("site_url", "expected"),
    [
        ("https://axion.example.com", "axion.example.com"),
        ("https://192.168.1.50", "192.168.1.50"),
        ("https://axion.example.com/", "axion.example.com"),
        # Mattermost supports subpaths: the path must not leak into the host
        ("https://axion.example.com/mattermost", "axion.example.com"),
        # nor the port, which would break http://<host>:51821
        ("https://axion.example.com:8443", "axion.example.com"),
        ("https://axion.example.com:8443/mm", "axion.example.com"),
        ("192.168.1.50", "192.168.1.50"),
        ("", ""),
    ],
)
def test_host_from_site_url(site_url: str, expected: str) -> None:
    assert host_from_site_url(site_url) == expected


def test_discover_deployment_strips_path_from_site_url(tmp_path: Path) -> None:
    """Regression: an MM_SITEURL with a subpath left the host as
    `example.com/mm`, which then produced `http://example.com/mm:51821`."""
    (tmp_path / "docker-compose.yml").write_text("services:\n  wireguard:\n    image: x\n")
    (tmp_path / ".env").write_text(
        "OLLAMA_MODEL=qwen2.5:1.5b\nMM_SITEURL=https://axion.example.com/mattermost\n"
    )
    facts = discover_deployment(tmp_path)
    assert facts.host == "axion.example.com"


def test_discover_deployment_missing_host_raises(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yml").write_text("services:\n  wireguard:\n    image: x\n")
    (tmp_path / ".env").write_text("OLLAMA_MODEL=qwen2.5:1.5b\n")
    with pytest.raises(ConfigError, match="host"):
        discover_deployment(tmp_path)


def test_discover_deployment_missing_model_raises(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yml").write_text("services:\n  wireguard:\n    image: x\n")
    (tmp_path / "wg.env").write_text("WG_HOST=192.168.1.50\n")
    with pytest.raises(ConfigError, match="model"):
        discover_deployment(tmp_path)


def test_discover_deployment_detects_host_variant(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path, wireguard_service="    image: x\n    network_mode: host\n")
    facts = discover_deployment(tmp_path)
    assert facts.wireguard_variant == WireguardVariant.HOST.value


def test_discover_deployment_cert_path_convention(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    facts = discover_deployment(tmp_path)
    assert facts.cert_path == tmp_path / "nginx" / "certs" / "cert.crt"


# --- unreadable compose: actionable error, never a raw traceback -------------------
#
# `detect_wireguard_variant_from_compose` sits on the path of EVERY `doctor`
# run. A corrupt YAML used to raise an uncaught `YAMLError` and surface through
# the generic handler as `Error inesperado: ...`, which is what §8 forbids.


def test_corrupt_compose_raises_an_actionable_config_error(tmp_path: Path) -> None:
    compose_path = tmp_path / "docker-compose.yml"
    compose_path.write_text("services: [this: is not: valid yaml\n", encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        detect_wireguard_variant_from_compose(compose_path)

    assert "Could not read" in excinfo.value.what
    assert excinfo.value.steps


def test_compose_without_a_root_mapping_raises_config_error(tmp_path: Path) -> None:
    compose_path = tmp_path / "docker-compose.yml"
    compose_path.write_text("- this is a list\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="shaped"):
        detect_wireguard_variant_from_compose(compose_path)


def test_discover_deployment_reports_a_corrupt_compose(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yml").write_text("{unclosed\n", encoding="utf-8")
    (tmp_path / ".env").write_text("OLLAMA_MODEL=x\nMM_SITEURL=https://h\n", encoding="utf-8")
    (tmp_path / "wg.env").write_text("WG_HOST=192.168.1.50\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        discover_deployment(tmp_path)


# --- env_value: the single .env reader --------------------------------------------


def test_env_value_reads_a_key(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("OLLAMA_MODEL=qwen2.5:3b\n", encoding="utf-8")
    assert env_value(tmp_path, "OLLAMA_MODEL") == "qwen2.5:3b"


def test_env_value_reads_from_another_file(tmp_path: Path) -> None:
    (tmp_path / "wg.env").write_text("WG_HOST=192.168.1.50\n", encoding="utf-8")
    assert env_value(tmp_path, "WG_HOST", filename="wg.env") == "192.168.1.50"


def test_env_value_missing_file_is_none(tmp_path: Path) -> None:
    assert env_value(tmp_path, "OLLAMA_MODEL") is None


def test_env_value_empty_value_is_none(tmp_path: Path) -> None:
    """An empty value is "not set", not the empty string: callers use `or` to
    fall back to a default, and `""` would silently defeat that."""
    (tmp_path / ".env").write_text("OLLAMA_MODEL=\n", encoding="utf-8")
    assert env_value(tmp_path, "OLLAMA_MODEL") is None


def test_env_value_undecodable_file_is_none(tmp_path: Path) -> None:
    """A `.env` saved as ANSI by Notepad must not abort the whole install —
    `OLLAMA_SYSTEM_PROMPT` invites hand-editing."""
    (tmp_path / ".env").write_bytes(b"OLLAMA_SYSTEM_PROMPT=s\xed muy bien\n")
    assert env_value(tmp_path, "OLLAMA_SYSTEM_PROMPT") is None
