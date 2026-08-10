"""Step 5 — Rendering the compose file and configuration files (§4.5).

Order of responsibilities:
1. Render `docker-compose.yml`, `.env`, `wg.env` and the nginx config from
   the packaged Jinja2 templates (`utils.resources`).
2. If `docker-compose.yml` already exists, back it up with a timestamp and
   edit it with `ruamel.yaml` rather than overwriting it, preserving any user
   customisation outside the services the wizard manages.
3. Validate the resulting YAML's shape and that the SSRF variable is present.
4. Write `.env`/`wg.env` with permissions restricted to the current user.
"""

from __future__ import annotations

import datetime
import io
import os
from collections.abc import MutableMapping
from pathlib import Path

import jinja2
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from axion_wizard.detect.docker import GPU_ACCELERATION_NONE
from axion_wizard.domain import images
from axion_wizard.domain.config import AxionConfig
from axion_wizard.domain.stack import MANAGED_SERVICES
from axion_wizard.errors import ConfigError
from axion_wizard.render.console import console
from axion_wizard.services.compose import config_validate
from axion_wizard.steps.base import Step, StepResult
from axion_wizard.utils import secrets as secret_utils
from axion_wizard.utils.fsperms import restrict_to_owner
from axion_wizard.utils.resources import read_template_text

SSRF_ENV_KEY = "MM_SERVICESETTINGS_ALLOWEDUNTRUSTEDINTERNALCONNECTIONS"
#: `n8n:5678` is always included, even when n8n is not installed: the list
#: only permits destinations, it does not require them to exist, and a
#: surplus entry costs nothing. A missing one does: Mattermost's SSRF
#: protection drops the outgoing webhook **silently**, with no error in any
#: log, and from the outside it looks as though n8n simply never hears about
#: anything.
SSRF_ENV_VALUE = "fastapi:8000 fastapi n8n:5678 n8n"

#: Directory where the `backup` service leaves its tar.gz files, relative to
#: the project.
BACKUPS_RELATIVE_DIR = Path("backups")

#: In the small hours, because the backup pauses PostgreSQL and Mattermost
#: while it archives.
DEFAULT_BACKUP_CRON_EXPRESSION = "0 3 * * *"
DEFAULT_BACKUP_RETENTION_DAYS = "7"

#: Seconds Mattermost waits for the outgoing webhook to answer. Mattermost's
#: own default is 30, which on CPU is not enough for even a small model: past
#: the deadline the answer is lost whole and without error. 180 comfortably
#: covers a long answer from a 3B model on CPU.
OUTGOING_WEBHOOK_TIMEOUT_SECONDS = "180"

#: n8n's timezone, so its cron jobs fire at the hour the user expects. It is
#: deliberately not inferred from the machine: n8n wants an IANA name
#: (`America/Argentina/Buenos_Aires`) and Windows supplies its own
#: ("Argentina Standard Time"), which n8n does not understand. Rather than
#: slipping in a value that would fail silently, it stays UTC and is
#: documented in the `.env`.
DEFAULT_N8N_TIMEZONE = "UTC"

ZONE_IDENTIFIER_SUFFIX = ":Zone.Identifier"

#: Prefix of the project name generated for a fresh install. See
#: `resolve_compose_project_name`.
PROJECT_NAME_PREFIX = "axion"


def _legacy_project_name_from_compose(project_dir: Path) -> str | None:
    """The top-level `name:` of an existing `docker-compose.yml`, if it has
    one.

    Only wizard versions predating the move of the project name into `.env`
    wrote it (they hardcoded `name: axion`, the same for *every* install — the
    cause of the bug `resolve_compose_project_name` exists to prevent). Its
    only purpose is backward migration: for a deployment carrying that
    `docker-compose.yml`, the same project name has to be kept or Compose
    would stop finding its existing containers and volumes.
    """
    compose_path = project_dir / "docker-compose.yml"
    try:
        text = compose_path.read_text(encoding="utf-8")
    except OSError:
        return None
    yaml = YAML(typ="safe")
    try:
        data = yaml.load(text)
    except YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    name = data.get("name")
    return name if isinstance(name, str) and name else None


def resolve_compose_project_name(project_dir: Path) -> str:
    """The Compose project name for this deployment: stable and, above all,
    unique.

    It is read from an existing `.env` when there is one, so it survives any
    later `install`. Failing that, the `name:` of a `docker-compose.yml` from
    an earlier wizard version is migrated if found (see
    `_legacy_project_name_from_compose`). Only when there is no clue at all —
    a genuinely new install — is one generated with a random suffix.

    The suffix is what matters: without it, *every* axion-wizard install on
    the same Docker host shares the project name `axion`, and Compose
    identifies a project by its name, not by the directory it was invoked
    from — so they share their containers and volumes too. This is a real
    incident, not a hypothetical: installing into a second folder generated a
    new PostgreSQL password and wrote it into a `.env` that, unknowingly,
    pointed at the same project as a running deployment with real data.
    Docker accepted the new password without complaint — it only applies when
    initialising an empty volume, and this one was no longer empty — and
    Mattermost was left authenticating against the old password with the new
    one written in its `.env`: a restart loop, with no log anywhere pointing
    out that the problem was a collision between two separate installs.
    """
    existing = existing_env_value(project_dir, "COMPOSE_PROJECT_NAME")
    if existing:
        return existing
    legacy = _legacy_project_name_from_compose(project_dir)
    if legacy:
        return legacy
    return f"{PROJECT_NAME_PREFIX}-{secret_utils.generate_hex_secret(4)}"


def assert_no_wg_easy_v14_volume(project_dir: Path) -> None:
    """Abort if this deployment was written by wg-easy v14.

    v15 cannot read v14's volume. Letting it start on top of one does not
    fail: it finds a store it does not recognise, launches its setup wizard
    and leaves an **empty** panel — with none of the previous clients. The
    tunnels of every already-configured device stop working at once, and
    nothing in the logs says there was data to migrate.

    Hence this stopping the install rather than warning and carrying on: by
    the end of the step it would already be too late, and anyone arriving here
    from an earlier wizard version has no reason to know the migration exists.
    The steps explain how to do it, and overwriting the folder remains
    possible on purpose, not by accident.
    """
    from axion_wizard.domain.deployment import wg_easy_major_in_compose
    from axion_wizard.domain.images import WG_EASY_MIN_SAFE_MAJOR

    compose_path = project_dir / "docker-compose.yml"
    major = wg_easy_major_in_compose(compose_path)
    if major is None or major >= WG_EASY_MIN_SAFE_MAJOR:
        return

    raise ConfigError(
        what=f"This deployment uses wg-easy v{major} and the wizard now installs v15",
        why=(
            "v15 does not read v14's configuration store. If it starts on top of this "
            "volume, it shows its setup wizard with an empty panel and every already "
            "enrolled WireGuard client stops connecting — with no error to explain "
            "it."
        ),
        steps=[
            f"Open the current panel (http://<host>:{51821}) and use its backup "
            "button to download `wg0.json`.",
            "Run `axion-wizard install` again: the v15 panel will come up in its "
            "setup wizard, where you choose that you already have a configuration "
            "and upload that `wg0.json`.",
            "If there are no clients worth keeping, delete the volume and start "
            "clean: axion-wizard uninstall --purge",
        ],
    )


def _render(template_name: str, context: dict) -> str:
    template_text = read_template_text(template_name)
    template = jinja2.Template(
        template_text, keep_trailing_newline=True, undefined=jinja2.StrictUndefined
    )
    return template.render(**context)


def build_compose_context(
    config: AxionConfig,
    gpu_acceleration: str = GPU_ACCELERATION_NONE,
) -> dict:
    return {
        "n8n_image": images.N8N_IMAGE,
        "ssrf_allowed_connections": SSRF_ENV_VALUE,
        "host": config.host,
        "wireguard_variant": config.wireguard_variant.value,
        "postgres_image": images.POSTGRES_IMAGE,
        "mattermost_image": images.MATTERMOST_IMAGE,
        "ollama_image": images.ollama_image_for(gpu_acceleration),
        "nginx_image": images.NGINX_IMAGE,
        "wireguard_image": images.WIREGUARD_IMAGE,
        "backup_image": images.BACKUP_IMAGE,
        "gpu_acceleration": gpu_acceleration,
        "outgoing_webhook_timeout": OUTGOING_WEBHOOK_TIMEOUT_SECONDS,
    }


def render_compose(config: AxionConfig, gpu_acceleration: str = GPU_ACCELERATION_NONE) -> str:
    return _render("docker-compose.yml.j2", build_compose_context(config, gpu_acceleration))


#: `.env` keys the user fills in *after* deployment, and which therefore have
#: to be carried over from the previous run rather than regenerated.
#: `POSTGRES_PASSWORD` is not here because it travels through `AxionConfig`
#: (step 3 already reads it from the existing `.env`; see
#: `s03_config.existing_postgres_password`).
PRESERVED_ENV_KEYS = (
    "MM_WEBHOOK_TOKEN",
    "MM_BOT_TOKEN",
    "OLLAMA_SYSTEM_PROMPT",
    "BACKUP_CRON_EXPRESSION",
    "BACKUP_RETENTION_DAYS",
    # Regenerating it makes EVERY credential stored in n8n unreadable, with no
    # way to recover them and nothing to warn you until a workflow fails to
    # authenticate.
    "N8N_ENCRYPTION_KEY",
    "N8N_TIMEZONE",
    # Only has an effect in asynchronous mode (with MM_BOT_TOKEN set); it is
    # asked for alongside the bot token in step 9.
    "AI_REPLY_IN_THREAD",
)

#: Regenerating the .env when the user has never chosen must not change the
#: behaviour they already had deployed.
DEFAULT_AI_REPLY_IN_THREAD = "true"


def existing_env_value(project_dir: Path, key: str) -> str | None:
    """The value of `key` in a previous run's `.env`, if there is one.

    An alias for `deployment.env_value`, which is the single place a `.env` is
    parsed. The name is kept because it is what the steps and the tests call
    it, and because here "existing" says what matters in this module: the
    value that was already there before the file is regenerated.
    """
    from axion_wizard.domain.deployment import env_value

    return env_value(project_dir, key)


def preserved_env_values(project_dir: Path) -> dict[str, str]:
    """The values of `PRESERVED_ENV_KEYS` already written into `.env`."""
    return {key: existing_env_value(project_dir, key) or "" for key in PRESERVED_ENV_KEYS}


def render_env(
    config: AxionConfig, compose_project_name: str, preserved: dict[str, str] | None = None
) -> str:
    """Render `.env`.

    `compose_project_name` is mandatory and has no default **on purpose**: it
    is what stops two separate installs on the same Docker host from ending up
    sharing containers and volumes (a real incident — see
    `resolve_compose_project_name`, which is what must compute it before
    calling here). A fixed default would reintroduce exactly that bug in any
    call that forgot to pass it.

    `preserved` carries the values the user configures *after* deployment and
    which this file cannot know in advance: the webhook token (Mattermost
    generates it on creation) and the AI's instructions. Regenerating the
    whole file with defaults meant a second `install` pass — to change the
    model, say — deleted them without a word; in the token's case that also
    meant fastapi went back to accepting any webhook call without validating
    it. An invisible security downgrade: no error, no warning, nothing in the
    logs.
    """
    preserved = preserved or {}
    return _render(
        "env.j2",
        {
            "compose_project_name": compose_project_name,
            "postgres_password": config.postgres_password.get_secret_value(),
            "ollama_model": config.ollama_model,
            "host": config.host,
            "mm_webhook_token": preserved.get("MM_WEBHOOK_TOKEN", ""),
            "mm_bot_token": preserved.get("MM_BOT_TOKEN", ""),
            "ollama_system_prompt": preserved.get("OLLAMA_SYSTEM_PROMPT", ""),
            # Unlike the tokens, an empty value is not valid here: Compose
            # would interpolate it as-is and the service would start with no
            # schedule and no retention. Hence the `or` with the default,
            # which covers both a first install and a `.env` somebody has
            # deleted the line from.
            "backup_cron_expression": (
                preserved.get("BACKUP_CRON_EXPRESSION") or DEFAULT_BACKUP_CRON_EXPRESSION
            ),
            "backup_retention_days": (
                preserved.get("BACKUP_RETENTION_DAYS") or DEFAULT_BACKUP_RETENTION_DAYS
            ),
            # Generated exactly once, the first time. From then on it arrives
            # via `preserved` and is never touched again.
            "n8n_encryption_key": (
                preserved.get("N8N_ENCRYPTION_KEY") or secret_utils.generate_hex_secret()
            ),
            "n8n_timezone": preserved.get("N8N_TIMEZONE") or DEFAULT_N8N_TIMEZONE,
            "ai_reply_in_thread": (
                preserved.get("AI_REPLY_IN_THREAD") or DEFAULT_AI_REPLY_IN_THREAD
            ),
        },
    )


def render_wg_env(config: AxionConfig) -> str:
    return _render(
        "wg.env.j2",
        {
            "host": config.host,
            "wireguard_admin_username": config.wireguard_admin_username,
            "wireguard_admin_password": config.wireguard_admin_password.get_secret_value(),
        },
    )


def render_nginx_conf(config: AxionConfig) -> str:
    return _render("nginx-mattermost.conf.j2", {"host": config.host})


def validate_compose_yaml_shape(compose_text: str) -> None:
    """Validate that the generated YAML is syntactically correct and has the
    minimum expected shape. The real semantic validation
    (`docker compose config --quiet`) requires Docker and runs in step 6 via
    `services.compose`."""
    yaml = YAML(typ="safe")
    data = yaml.load(compose_text)
    if not isinstance(data, dict) or "services" not in data:
        raise ConfigError(
            what="The generated docker-compose.yml is not shaped as expected",
            why="The top-level `services` key is missing; the generated file is corrupt.",
            steps=["Report this error — it is a wizard bug, not something you did."],
        )
    services = data["services"]
    missing = [name for name in MANAGED_SERVICES if name not in services]
    if missing:
        raise ConfigError(
            what=f"Managed services missing from docker-compose.yml: {', '.join(missing)}",
            why="The generated file is incomplete and the deployment would fail.",
            steps=["Report this error — it is a wizard bug, not something you did."],
        )


def assert_ssrf_env_present(compose_text: str) -> None:
    """The user should never have to know this variable exists (§12): the
    wizard always injects it and aborts if for any reason it did not land."""
    if SSRF_ENV_VALUE not in compose_text:
        raise ConfigError(
            what="The SSRF protection variable is missing from the mattermost service",
            why=(
                f'Without `{SSRF_ENV_KEY}: "{SSRF_ENV_VALUE}"`, Mattermost\'s SSRF '
                "protection blocks the webhook to fastapi:8000 silently: no error "
                "appears in any log, the webhook simply never fires."
            ),
            steps=["Report this error — it is a wizard bug, not something you did."],
        )


def assert_no_unpinned_images(compose_text: str) -> None:
    for image in images.ALL_PINNED_IMAGES:
        images.assert_image_is_pinned(image)
    if ":latest" in compose_text:
        raise ConfigError(
            what="The generated docker-compose.yml references an image tagged `latest`",
            why=(
                "The pinned tags exist for reproducible builds and to avoid breaking "
                "wg-easy (§6.4)."
            ),
            steps=["Report this error — it is a wizard bug, not something you did."],
        )


def backup_existing_file(path: Path) -> Path | None:
    """If `path` already exists, copy it with a timestamp suffix before
    touching it.

    The timestamp has one-second resolution, so two backups in quick
    succession (a retried `install`, say) would land on the same name and the
    second would overwrite the first — precisely the copy this mechanism
    exists to keep. If the name is already taken, a counter is appended.
    """
    if not path.exists():
        return None
    timestamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")

    backup_path = path.with_name(f"{path.name}.{timestamp}.bak")
    counter = 2
    while backup_path.exists():
        backup_path = path.with_name(f"{path.name}.{timestamp}-{counter}.bak")
        counter += 1

    backup_path.write_bytes(path.read_bytes())
    return backup_path


def merge_compose_preserving_user_edits(existing_text: str, rendered_text: str) -> str:
    """Replace only the services in `MANAGED_SERVICES` and the
    `volumes`/`networks` keys the wizard manages, preserving any other
    service, top-level key or comment the user hand-added to the existing
    `docker-compose.yml`."""
    yaml = YAML()
    yaml.preserve_quotes = True

    try:
        existing = yaml.load(existing_text)
    except YAMLError as exc:
        raise ConfigError(
            what="The existing docker-compose.yml is not valid YAML",
            why=(
                "The wizard backs up and merges the existing file rather than "
                f"overwriting it, but it could not be parsed: {exc}"
            ),
            steps=[
                "Fix the syntax of the current docker-compose.yml, or",
                "Move/rename it so the wizard generates a clean one.",
            ],
        ) from exc

    rendered = yaml.load(rendered_text)

    if existing is None:
        existing = {}
    # A valid compose file is always a mapping at the root. If it is not (a
    # list, a scalar…), indexing it below would blow up with a raw TypeError
    # instead of an actionable message.
    if not isinstance(existing, MutableMapping):
        raise ConfigError(
            what="The existing docker-compose.yml has no mapping at its root",
            why=(
                f"Found {type(existing).__name__} where Compose expects a mapping "
                "with keys such as `services:`. The file is not a valid "
                "docker-compose.yml and cannot be merged safely."
            ),
            steps=[
                "Review the current docker-compose.yml, or",
                "Move/rename it so the wizard generates a clean one.",
            ],
        )

    if not isinstance(existing.get("services"), MutableMapping):
        existing["services"] = {}

    for name in MANAGED_SERVICES:
        if name in rendered.get("services", {}):
            existing["services"][name] = rendered["services"][name]

    for top_level_key in ("volumes", "networks"):
        rendered_section = rendered.get(top_level_key)
        if rendered_section is None:
            continue
        if not isinstance(existing.get(top_level_key), MutableMapping):
            existing[top_level_key] = rendered_section
        else:
            for key, value in rendered_section.items():
                existing[top_level_key].setdefault(key, value)

    buffer = io.StringIO()
    yaml.dump(existing, buffer)
    return buffer.getvalue()


def render_compose_to_disk(
    config: AxionConfig, compose_path: Path, gpu_acceleration: str = GPU_ACCELERATION_NONE
) -> Path | None:
    """Render `docker-compose.yml`, backing up and merging if it already
    existed. Returns the path of the backup created, or `None` if the file was
    new."""
    rendered = render_compose(config, gpu_acceleration=gpu_acceleration)
    validate_compose_yaml_shape(rendered)
    assert_ssrf_env_present(rendered)
    assert_no_unpinned_images(rendered)

    backup_path = backup_existing_file(compose_path)
    if backup_path is not None:
        existing_text = backup_path.read_text(encoding="utf-8")
        final_text = merge_compose_preserving_user_edits(existing_text, rendered)
    else:
        final_text = rendered

    compose_path.parent.mkdir(parents=True, exist_ok=True)
    compose_path.write_text(final_text, encoding="utf-8")
    return backup_path


def write_secret_env_file(path: Path, content: str, *, backup: bool = False) -> Path | None:
    """Write a file containing secrets (`.env`, `wg.env`) restricted to the
    current user (§4.5, §9). Returns the backup's path, if one was requested
    and there was something to back up.

    `backup` exists because these two files are regenerated in full on every
    `install` and are exactly where users end up hand-editing things (an
    `MM_WEBHOOK_TOKEN`, a wg-easy setting). `docker-compose.yml` was already
    backed up; these were not, and they were the only ones with secrets
    inside.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = backup_existing_file(path) if backup else None
    path.write_text(content, encoding="utf-8")
    restrict_to_owner(path)
    if backup_path is not None:
        # The backup inherits the original's secrets, so it inherits its
        # permissions too: otherwise `.env` ends up restricted and its copy
        # right beside it does not.
        restrict_to_owner(backup_path)
    return backup_path


def update_env_value(path: Path, key: str, value: str) -> None:
    """Update (or add) a key in an existing `.env`, leaving every other line
    — comments, order, other keys — intact.

    It exists for values that are not known at deploy time and get filled in
    by hand afterwards, such as `MM_WEBHOOK_TOKEN` (Mattermost generates it
    when the outgoing webhook is created, with the stack already running —
    §4.5 cannot write it in advance). Rewriting the whole file from scratch
    would lose any manual adjustment the user has made.
    """
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    prefix = f"{key}="
    new_line = f"{prefix}{value}"

    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = new_line
            break
    else:
        lines.append(new_line)

    write_secret_env_file(path, "\n".join(lines) + "\n")


def ensure_gitignore_entries(project_dir: Path) -> bool:
    """Add `.env`, `wg.env`, `nginx/certs/` and `.axion-wizard-state.json` to
    `.gitignore` if there is a repository. Returns `True` if anything
    changed."""
    git_dir = project_dir / ".git"
    if not git_dir.exists():
        return False

    # `backups/` carries database dumps and the WireGuard keys: committing it
    # would leak everything `.env` protects.
    required_entries = [
        ".env",
        "wg.env",
        "nginx/certs/",
        ".axion-wizard-state.json",
        "backups/",
    ]
    gitignore_path = project_dir / ".gitignore"
    existing_lines = (
        gitignore_path.read_text(encoding="utf-8").splitlines() if gitignore_path.exists() else []
    )
    existing_set = {line.strip() for line in existing_lines}

    missing_entries = [entry for entry in required_entries if entry not in existing_set]
    if not missing_entries:
        return False

    with gitignore_path.open("a", encoding="utf-8") as handle:
        if existing_lines and existing_lines[-1].strip():
            handle.write("\n")
        handle.write("# added by axion-wizard\n")
        for entry in missing_entries:
            handle.write(f"{entry}\n")
    return True


#: Files of the FastAPI bridge that the compose file builds from `./fastapi`
#: (`build.context`). They ship inside the binary and are copied verbatim.
FASTAPI_TEMPLATE_FILES = ("Dockerfile", "main.py", "requirements.txt")

NGINX_CONF_RELATIVE_PATH = Path("nginx") / "nginx-mattermost.conf"


def write_fastapi_sources(project_dir: Path) -> list[Path]:
    """Dump `templates/fastapi/` into `<project_dir>/fastapi/`.

    The compose file declares `build.context: ./fastapi`, so without these
    files `docker compose up --build` fails with a context error that says
    nothing about what actually happened (that the wizard did not write them).
    """
    target_dir = project_dir / "fastapi"
    target_dir.mkdir(parents=True, exist_ok=True)

    written = []
    for filename in FASTAPI_TEMPLATE_FILES:
        content = read_template_text(f"fastapi/{filename}")
        path = target_dir / filename
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


#: Directories not walked when hunting for `:Zone.Identifier`. None of them
#: can contain one that matters, and `.venv`/`node_modules` hold tens of
#: thousands of files: walking them turned an instant cleanup into a
#: multi-second pause in the middle of the step that writes everything.
_UNSCANNED_DIRECTORY_NAMES = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}
)


def clean_zone_identifier_files(project_dir: Path) -> tuple[list[Path], list[Path]]:
    """Delete `*:Zone.Identifier` files (the "downloaded from the internet"
    metadata Windows leaves on files copied in from outside WSL).

    Returns `(removed, not_removed)`. A file that refuses to be deleted —
    locked by another process, no permissions — does **not** abort the step:
    it is cosmetic litter, and bringing down the step that writes the compose
    file, the `.env` and the certificate over it would be disproportionate.
    The `OSError` used to travel up to the generic handler and come out as
    `Unexpected error`.

    Uses `os.walk` rather than `Path.glob`/`rglob`: in pathlib a pattern with
    a `:` is read as a drive prefix (`C:`), and `glob("*:Zone.Identifier")`
    raises `NotImplementedError: Non-relative patterns are unsupported`.
    """
    removed: list[Path] = []
    failed: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(project_dir):
        # In-place pruning: `os.walk` respects mutation of `dirnames`.
        dirnames[:] = [name for name in dirnames if name not in _UNSCANNED_DIRECTORY_NAMES]
        for filename in filenames:
            if not filename.endswith(ZONE_IDENTIFIER_SUFFIX):
                continue
            path = Path(dirpath) / filename
            try:
                path.unlink()
            except OSError:
                failed.append(path)
            else:
                removed.append(path)
    return removed, failed


class ComposeStep(Step):
    """Write every project artifact and validate the compose file (§4.5).

    It is the first step that touches the disk: up to here the flow has only
    looked and asked. Hence step 3's confirmation being the point of no
    return, and this step backing up whatever was already there.
    """

    name = "compose"
    title = "Compose and configuration files"

    def run(self) -> StepResult:
        config = self.context.require_config()
        environment = self.context.require_environment()
        compose_path = self.context.project_dir / "docker-compose.yml"

        if self.state.dry_run:
            self._announce_dry_run(compose_path)
            return StepResult(name=self.name, ok=True, message="skipped by --dry-run")

        # The next two checks read `docker-compose.yml` as it stands *now*,
        # before `render_compose_to_disk` overwrites it: afterwards there is no
        # record left of what it was deployed with.
        assert_no_wg_easy_v14_volume(self.context.project_dir)
        project_name = resolve_compose_project_name(self.context.project_dir)

        backup_path = render_compose_to_disk(
            config, compose_path, gpu_acceleration=environment.gpu_acceleration
        )

        # The values the user fills in after deployment (webhook token, AI
        # instructions) are carried over from the previous run: regenerating
        # `.env` with defaults deleted them silently.
        preserved = preserved_env_values(self.context.project_dir)
        write_secret_env_file(
            self.context.project_dir / ".env",
            render_env(config, project_name, preserved=preserved),
            backup=True,
        )
        write_secret_env_file(
            self.context.project_dir / "wg.env", render_wg_env(config), backup=True
        )
        kept = [key for key, value in preserved.items() if value]
        if kept:
            console.print(f"[axion.dim]Kept from the previous .env: {', '.join(kept)}.[/]")

        nginx_conf_path = self.context.project_dir / NGINX_CONF_RELATIVE_PATH
        nginx_conf_path.parent.mkdir(parents=True, exist_ok=True)
        nginx_conf_path.write_text(render_nginx_conf(config), encoding="utf-8")

        write_fastapi_sources(self.context.project_dir)
        # Created here rather than left to Docker: a bind mount pointing at a
        # path that does not exist gets created by `root`, and then the
        # backups are written by root into a folder the user can neither read
        # nor delete.
        (self.context.project_dir / BACKUPS_RELATIVE_DIR).mkdir(parents=True, exist_ok=True)
        ensure_gitignore_entries(self.context.project_dir)

        if environment.wsl.inside_wsl:
            removed, failed = clean_zone_identifier_files(self.context.project_dir)
            if removed:
                console.print(
                    f"[axion.dim]Cleaned up {len(removed)} :Zone.Identifier files.[/]"
                )
            if failed:
                console.print(
                    f"[axion.dim]{len(failed)} :Zone.Identifier files could not be "
                    "deleted (locked or no permissions); this does not affect the "
                    "deployment.[/]"
                )

        # The real semantic validation, with Docker: the shape check was
        # already done by `render_compose_to_disk` on the rendered text.
        config_validate(compose_path)

        if backup_path is not None:
            console.print(f"[axion.info]Previous compose backed up at:[/] {backup_path}")
        console.print(f"[axion.ok]Files written to:[/] {self.context.project_dir}")
        console.print(
            f"[axion.dim]Compose project name: {project_name} "
            "(it prefixes containers and volumes; do not share it with another "
            "deployment).[/]"
        )

        return StepResult(
            name=self.name,
            ok=True,
            data={"backup": str(backup_path) if backup_path else ""},
            message=f"compose and .env generated in {self.context.project_dir}",
        )

    def verify(self) -> StepResult:
        if self.state.dry_run:
            return StepResult(name=self.name, ok=True, message="skipped by --dry-run")

        missing = [
            str(relative)
            for relative in (
                Path("docker-compose.yml"),
                Path(".env"),
                Path("wg.env"),
                NGINX_CONF_RELATIVE_PATH,
                Path("fastapi") / "Dockerfile",
            )
            if not (self.context.project_dir / relative).exists()
        ]
        if missing:
            return StepResult(
                name=self.name, ok=False, message=f"missing files: {', '.join(missing)}"
            )
        return StepResult(name=self.name, ok=True, message="every file present")

    def _announce_dry_run(self, compose_path: Path) -> None:
        console.print(f"[axion.info][dry-run][/] would write {compose_path}")
        for relative in (".env", "wg.env", str(NGINX_CONF_RELATIVE_PATH)):
            target = self.context.project_dir / relative
            console.print(f"[axion.info][dry-run][/] would write {target}")
        console.print(
            f"[axion.info][dry-run][/] would copy the FastAPI bridge to "
            f"{self.context.project_dir / 'fastapi'}"
        )
        console.print(
            "[axion.info][dry-run][/] would validate with `docker compose config --quiet`"
        )
