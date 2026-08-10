"""Reading back what is already deployed, from the artifacts on disk.

`.env`, `wg.env` and `docker-compose.yml` are the only durable record of a
deployment's shape. The persisted wizard state deliberately holds none of it
(§9: passwords travel here), so anything that needs to know the host, the
model or the WireGuard variant has to reconstruct it by reading those files.

Three callers need exactly that and none of them is step 9:

- `doctor` diagnoses a stack it did not install in this session,
- `wireguard add-client` needs the host to build the panel URL,
- `s03_config.restore()` rebuilds `AxionConfig` when resuming a run.

All three used to import it from `s09_verify` — two of them reaching for
underscore-private names across a module boundary — which made step 9 a
library that also happens to be a step. This module is that library.

`env_value` is the single place that parses `.env`. There were four
independent `dotenv_values` readers before, each with its own idea of what
a missing or unreadable file means.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from axion_wizard.config import WireguardVariant
from axion_wizard.errors import ConfigError
from axion_wizard.stack import WIREGUARD_SERVICE

COMPOSE_FILENAME = "docker-compose.yml"
CERT_RELATIVE_PATH = Path("nginx") / "certs" / "cert.crt"


@dataclass
class DeploymentFacts:
    project_dir: Path
    compose_path: Path
    cert_path: Path
    host: str
    ollama_model: str
    wireguard_variant: str


def env_value(project_dir: Path, key: str, filename: str = ".env") -> str | None:
    """Value of `key` in a previously written env file, or `None`.

    The single point where already-written values are read, so that whoever
    regenerates a file does not need to know how it is parsed.

    An unreadable file is treated as "no previous value" rather than
    exploding: the wizard always writes UTF-8, but `OLLAMA_SYSTEM_PROMPT`
    invites hand-editing, and Notepad saving as ANSI produces bytes that
    `dotenv_values` cannot decode. Losing a value that can no longer be read
    is bad; aborting the whole install over it is worse.
    """
    try:
        return dotenv_values(project_dir / filename).get(key) or None
    except (OSError, UnicodeDecodeError):
        return None


def host_from_site_url(site_url: str) -> str:
    """Just the host of an `MM_SITEURL` — no scheme, port or path.

    Mattermost supports subpath deployments (`https://example.com/mm`), so
    keeping "everything after `://`" would drag the path and port into the
    host identifier — which is then concatenated to form, among others, the
    WireGuard panel URL (`http://<host>:51821`).
    """
    site_url = site_url.strip()
    if not site_url:
        return ""
    # `urlsplit` only recognises the authority when there is a scheme; if the
    # value arrives bare (`192.168.1.50`) we add one so it parses the same.
    to_parse = site_url if "://" in site_url else f"//{site_url}"
    try:
        hostname = urlsplit(to_parse).hostname
    except ValueError:
        return site_url.split("://", 1)[-1].split("/", 1)[0].strip()
    return hostname or ""


def detect_wireguard_variant_from_compose(compose_path: Path) -> str:
    """Infer the variant from the wireguard service's `network_mode`.

    Read/parse errors become `ConfigError`: this function sits on the path of
    *every* `doctor` run, and a corrupt or unreadable `docker-compose.yml`
    used to surface through the generic handler as `Error inesperado: …`,
    which is exactly what §8 forbids.
    """
    yaml = YAML(typ="safe")
    try:
        data = yaml.load(compose_path.read_text(encoding="utf-8")) or {}
    except (YAMLError, OSError, UnicodeDecodeError) as exc:
        raise ConfigError(
            what=f"Could not read {compose_path}",
            why=(
                "The file exists but could not be parsed as YAML, so there is no way "
                f"to tell how the stack is deployed: {exc}"
            ),
            steps=[
                "Check the syntax of docker-compose.yml.",
                "Restore a backup (docker-compose.yml.*.bak) if there is one.",
                "Or regenerate it with `axion-wizard install`.",
            ],
        ) from exc

    if not isinstance(data, dict):
        raise ConfigError(
            what=f"{compose_path} is not shaped like a docker-compose.yml",
            why=(
                f"Found {type(data).__name__} at the root where Compose expects a "
                "mapping with keys such as `services:`."
            ),
            steps=["Regenerate the file with `axion-wizard install`."],
        )

    services = data.get("services")
    wireguard_service = services.get(WIREGUARD_SERVICE) if isinstance(services, dict) else None
    if isinstance(wireguard_service, dict) and wireguard_service.get("network_mode") == "host":
        return WireguardVariant.HOST.value
    return WireguardVariant.PORTS.value


def discover_deployment(project_dir: Path) -> DeploymentFacts:
    """Rebuild host/model/variant by reading `docker-compose.yml`, `.env` and
    `wg.env` from `project_dir` — without this, `doctor` could not run against
    a stack unless the `install` that created it had run in this same session.
    """
    compose_path = project_dir / COMPOSE_FILENAME
    if not compose_path.exists():
        raise ConfigError(
            what=f"Could not find {compose_path}",
            why="`doctor` needs a stack already deployed by `axion-wizard install`.",
            steps=[
                "Run `axion-wizard install` first.",
                "Or point at the right directory with --project-dir.",
            ],
        )

    host = env_value(project_dir, "WG_HOST", filename="wg.env") or host_from_site_url(
        env_value(project_dir, "MM_SITEURL") or ""
    )
    if not host:
        raise ConfigError(
            what="Could not determine the access host",
            why="Neither wg.env (WG_HOST) nor .env (MM_SITEURL) holds a usable value.",
            steps=["Check that .env and wg.env are not corrupt or empty."],
        )

    ollama_model = env_value(project_dir, "OLLAMA_MODEL")
    if not ollama_model:
        raise ConfigError(
            what="Could not determine the configured Ollama model",
            why="`.env` has no OLLAMA_MODEL variable.",
            steps=["Check that .env is not corrupt or incomplete."],
        )

    return DeploymentFacts(
        project_dir=project_dir,
        compose_path=compose_path,
        cert_path=project_dir / CERT_RELATIVE_PATH,
        host=host,
        ollama_model=ollama_model,
        wireguard_variant=detect_wireguard_variant_from_compose(compose_path),
    )
