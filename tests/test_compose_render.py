import functools
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from axion_wizard.domain import images
from axion_wizard.domain.config import AccessMode, AxionConfig, WireguardVariant
from axion_wizard.domain.stack import MANAGED_SERVICES
from axion_wizard.errors import ConfigError
from axion_wizard.services import compose as compose_service
from axion_wizard.steps import s05_compose as s05
from axion_wizard.utils.secrets import generate_hex_secret


def make_config(**overrides) -> AxionConfig:
    kwargs = dict(
        access_mode=AccessMode.LAN,
        host="192.168.1.50",
        wireguard_variant=WireguardVariant.PORTS,
        postgres_password=generate_hex_secret(),
        wireguard_admin_username="admin",
        wireguard_admin_password="correct-horse-battery-staple",
        ollama_model="qwen2.5:1.5b",
        project_dir=Path("."),
    )
    kwargs.update(overrides)
    return AxionConfig(**kwargs)


def _load_yaml(text: str) -> dict:
    return YAML(typ="safe").load(text)


#: A fixed project name for the tests: `render_env` demands one with no
#: default on purpose (see its docstring), so something has to be passed.
TEST_PROJECT_NAME = "axion-test"


def render_env(config: AxionConfig, **kwargs) -> str:
    """Shortcut for `s05.render_env` with the project name already supplied."""
    return s05.render_env(config, TEST_PROJECT_NAME, **kwargs)


# --- render_compose: general shape ------------------------------------------


def test_render_compose_is_valid_yaml_with_managed_services() -> None:
    data = _load_yaml(s05.render_compose(make_config()))
    for name in MANAGED_SERVICES:
        assert name in data["services"]


def test_render_compose_uses_pinned_image_tags() -> None:
    text = s05.render_compose(make_config())
    assert images.POSTGRES_IMAGE in text
    assert images.MATTERMOST_IMAGE in text
    assert images.OLLAMA_IMAGE in text
    assert images.NGINX_IMAGE in text
    assert images.WIREGUARD_IMAGE in text
    assert ":latest" not in text


def test_render_compose_raises_the_outgoing_webhook_timeout() -> None:
    """Mattermost's default 30 s is not enough for a model on CPU: once it
    expires the answer is lost whole and no error appears in any log."""
    mattermost = _load_yaml(s05.render_compose(make_config()))["services"]["mattermost"]
    timeout = mattermost["environment"]["MM_SERVICESETTINGS_OUTGOINGINTEGRATIONREQUESTSTIMEOUT"]

    assert int(timeout) >= 120, "any less rules out 3B models on CPU"


def test_render_compose_passes_the_thread_preference_to_fastapi_with_a_default() -> None:
    fastapi = _load_yaml(s05.render_compose(make_config()))["services"]["fastapi"]
    assert fastapi["environment"]["AI_REPLY_IN_THREAD"] == "${AI_REPLY_IN_THREAD:-true}"


def test_render_compose_injects_ssrf_env_var() -> None:
    text = s05.render_compose(make_config())
    assert f'{s05.SSRF_ENV_KEY}: "{s05.SSRF_ENV_VALUE}"' in text


# --- n8n (native, no flag) ----------------------------------------------------


def test_n8n_is_included_natively() -> None:
    """There is no flag: n8n ships with the rest of the stack on every install."""
    assert "n8n" in MANAGED_SERVICES


def test_n8n_is_rendered_with_a_pinned_image_and_its_volume() -> None:
    data = _load_yaml(s05.render_compose(make_config()))
    n8n = data["services"]["n8n"]

    assert n8n["image"] == images.N8N_IMAGE
    assert ":latest" not in n8n["image"]
    assert "n8n_data" in data["volumes"]
    assert "5678:5678" in n8n["ports"]


def test_n8n_is_announced_over_http_because_nothing_terminates_tls_for_it() -> None:
    """n8n is published on its own port, not behind nginx. Announcing itself
    as https would make it generate webhook URLs that do not answer."""
    n8n = _load_yaml(s05.render_compose(make_config()))["services"]["n8n"]

    assert n8n["environment"]["N8N_PROTOCOL"] == "http"
    assert n8n["environment"]["WEBHOOK_URL"].startswith("http://")


def test_mattermost_is_allowed_to_reach_n8n() -> None:
    """Mattermost's SSRF protection drops the outgoing webhook silently: it
    does not fire and no error appears in any log."""
    assert "n8n:5678" in s05.SSRF_ENV_VALUE
    assert s05.SSRF_ENV_VALUE in s05.render_compose(make_config())


def test_backup_covers_n8n() -> None:
    """Without this, a restore would bring back the whole chat and leave n8n empty."""
    services = _load_yaml(s05.render_compose(make_config()))["services"]
    sources = {entry.split(":")[0] for entry in services["backup"]["volumes"]}

    assert "n8n_data" in sources


def test_n8n_encryption_key_is_generated_once_and_then_preserved() -> None:
    """If that key changes, the credentials stored in n8n become unreadable
    forever and nothing warns you until a workflow fails."""
    first = render_env(make_config())
    prefix = "N8N_ENCRYPTION_KEY="
    key = next(line[len(prefix) :] for line in first.splitlines() if line.startswith(prefix))
    assert key

    again = render_env(make_config(), preserved={"N8N_ENCRYPTION_KEY": key})
    assert f"N8N_ENCRYPTION_KEY={key}" in again


def test_merge_regenerates_n8n() -> None:
    existing = s05.render_compose(make_config())
    merged = _load_yaml(
        s05.merge_compose_preserving_user_edits(existing, s05.render_compose(make_config()))
    )
    assert "n8n" in merged["services"]


# --- the backup service -----------------------------------------------------


def test_backup_service_archives_the_volumes_that_matter_read_only() -> None:
    backup = _load_yaml(s05.render_compose(make_config()))["services"]["backup"]
    mounted = {entry.split(":")[0]: entry for entry in backup["volumes"]}

    for volume in (
        "postgres_data",
        "mattermost_data",
        "mattermost_config",
        "wireguard_data",
    ):
        assert volume in mounted, f"{volume} is not being backed up"
        assert mounted[volume].endswith(":ro"), f"{volume} must be mounted read-only"


def test_backup_service_excludes_the_model_and_the_logs() -> None:
    """`ollama_data` is gigabytes that can be re-pulled and logs are not
    restorable: including them would multiply each backup's size for
    nothing."""
    backup = _load_yaml(s05.render_compose(make_config()))["services"]["backup"]
    sources = {entry.split(":")[0] for entry in backup["volumes"]}

    assert "ollama_data" not in sources
    assert "mattermost_logs" not in sources


def test_backup_stops_postgres_and_mattermost_with_a_matching_label() -> None:
    """The containers' label and the value the service looks for have to match
    exactly: otherwise nothing is stopped and PostgreSQL is backed up live,
    without a single warning."""
    services = _load_yaml(s05.render_compose(make_config()))["services"]
    expected = services["backup"]["environment"]["BACKUP_STOP_DURING_BACKUP_LABEL"]

    for name in ("postgres", "mattermost"):
        assert services[name]["labels"]["docker-volume-backup.stop-during-backup"] == expected


def test_backup_prunes_only_its_own_archives() -> None:
    """Without a prefix, age-based pruning would reach any file in the
    destination."""
    backup = _load_yaml(s05.render_compose(make_config()))["services"]["backup"]
    prefix = backup["environment"]["BACKUP_PRUNING_PREFIX"]

    assert prefix
    assert backup["environment"]["BACKUP_FILENAME"].startswith(prefix)


def test_env_gets_backup_defaults_on_a_fresh_install() -> None:
    text = render_env(make_config())
    assert f"BACKUP_CRON_EXPRESSION={s05.DEFAULT_BACKUP_CRON_EXPRESSION}" in text
    assert f"BACKUP_RETENTION_DAYS={s05.DEFAULT_BACKUP_RETENTION_DAYS}" in text


def test_env_keeps_a_customised_backup_schedule() -> None:
    text = render_env(make_config(), preserved={"BACKUP_CRON_EXPRESSION": "30 4 * * 0"})
    assert "BACKUP_CRON_EXPRESSION=30 4 * * 0" in text


def test_env_falls_back_to_defaults_when_the_previous_value_is_empty() -> None:
    """An empty value would be interpolated as-is and leave the service with
    no schedule and no retention — unlike the tokens, empty is not a valid
    value here."""
    text = render_env(
        make_config(), preserved={"BACKUP_CRON_EXPRESSION": "", "BACKUP_RETENTION_DAYS": ""}
    )
    assert f"BACKUP_CRON_EXPRESSION={s05.DEFAULT_BACKUP_CRON_EXPRESSION}" in text
    assert f"BACKUP_RETENTION_DAYS={s05.DEFAULT_BACKUP_RETENTION_DAYS}" in text


def test_env_defaults_to_threaded_replies_on_a_fresh_install() -> None:
    """It preserves the behaviour already deployed before this setting
    existed: change nothing for anyone who chooses nothing."""
    text = render_env(make_config())
    assert f"AI_REPLY_IN_THREAD={s05.DEFAULT_AI_REPLY_IN_THREAD}" in text


def test_env_keeps_a_customised_thread_preference() -> None:
    text = render_env(make_config(), preserved={"AI_REPLY_IN_THREAD": "false"})
    assert "AI_REPLY_IN_THREAD=false" in text


# --- variante WireGuard: host vs ports --------------------------------------


def test_render_compose_host_variant_uses_network_mode_host() -> None:
    config = make_config(wireguard_variant=WireguardVariant.HOST)
    data = _load_yaml(s05.render_compose(config))
    wg = data["services"]["wireguard"]
    assert wg.get("network_mode") == "host"
    assert "ports" not in wg
    assert "sysctls" not in wg
    assert "networks" not in wg


def test_render_compose_ports_variant_publishes_ports_and_sysctls() -> None:
    config = make_config(wireguard_variant=WireguardVariant.PORTS)
    data = _load_yaml(s05.render_compose(config))
    wg = data["services"]["wireguard"]
    assert "network_mode" not in wg
    assert "51820:51820/udp" in wg["ports"]
    assert "51821:51821/tcp" in wg["ports"]
    assert wg["sysctls"]["net.ipv4.conf.all.src_valid_mark"] == 1
    assert "edge_net" in wg["networks"]


def test_render_compose_ports_variant_disables_ipv6_by_default() -> None:
    """Not called `wireguard_ipv6_supported` is the same "assume broken until
    proven otherwise" default as `gpu_acceleration`: whoever renders without
    threading step 1's probe through gets the safe setting, not the one that
    reproduces the incident."""
    config = make_config(wireguard_variant=WireguardVariant.PORTS)
    data = _load_yaml(s05.render_compose(config))
    assert data["services"]["wireguard"]["environment"]["DISABLE_IPV6"] == "true"


def test_render_compose_ports_variant_keeps_ipv6_when_the_probe_says_it_works() -> None:
    """When step 1's real probe (`docker_ipv6_netfilter_works`) confirms the
    kernel can run IPv6 netfilter, nothing needs disabling."""
    config = make_config(wireguard_variant=WireguardVariant.PORTS)
    data = _load_yaml(s05.render_compose(config, wireguard_ipv6_supported=True))
    assert "environment" not in data["services"]["wireguard"]


def test_render_compose_host_variant_ignores_ipv6_support() -> None:
    """`network_mode: host` has no IPv6 handling to switch off in the first
    place, so the flag makes no difference to this branch either way."""
    config = make_config(wireguard_variant=WireguardVariant.HOST)
    for supported in (True, False):
        data = _load_yaml(s05.render_compose(config, wireguard_ipv6_supported=supported))
        assert "environment" not in data["services"]["wireguard"]


def test_render_compose_nvidia_adds_deploy_reservation() -> None:
    data = _load_yaml(s05.render_compose(make_config(), gpu_acceleration="nvidia"))
    ollama = data["services"]["ollama"]
    assert "deploy" in ollama
    assert ollama["image"] == images.OLLAMA_IMAGE


def test_render_compose_no_gpu_omits_deploy_reservation() -> None:
    data = _load_yaml(s05.render_compose(make_config(), gpu_acceleration="none"))
    ollama = data["services"]["ollama"]
    assert "deploy" not in ollama
    assert "devices" not in ollama


def test_render_compose_rocm_swaps_the_image_and_passes_kernel_devices() -> None:
    """Both together or neither is any use: the default image ignores
    `/dev/kfd`, and the ROCm one without the devices has nothing to use."""
    data = _load_yaml(s05.render_compose(make_config(), gpu_acceleration="rocm"))
    ollama = data["services"]["ollama"]

    assert ollama["image"] == images.OLLAMA_ROCM_IMAGE
    assert ollama["devices"] == ["/dev/kfd", "/dev/dri"]
    assert ollama["group_add"] == ["video", "render"]
    assert "deploy" not in ollama


def test_render_compose_has_no_project_name_of_its_own() -> None:
    """The project name lives in `.env` (`COMPOSE_PROJECT_NAME`), not in the
    compose file: pinning it here as a literal was what made *every*
    axion-wizard install on the same Docker host share one project — same
    containers, same volumes — regardless of the folder. See
    `resolve_compose_project_name`."""
    data = _load_yaml(s05.render_compose(make_config()))
    assert "name" not in data


# --- resolve_compose_project_name --------------------------------------------
#
# The real bug this function exists to prevent: without a unique name per
# deployment, two axion-wizard installs on the same Docker host share one
# Compose project — same containers, same volumes — and each `install`
# silently overwrites the other's configuration. It really happened: a second
# install generated a different PostgreSQL password and left Mattermost unable
# to authenticate against a volume already initialised with the old one.


def test_project_name_is_stable_across_renders_of_the_same_deployment(tmp_path: Path) -> None:
    first = s05.resolve_compose_project_name(tmp_path)
    (tmp_path / ".env").write_text(f"COMPOSE_PROJECT_NAME={first}\n", encoding="utf-8")
    assert s05.resolve_compose_project_name(tmp_path) == first


def test_two_fresh_deployments_never_get_the_same_project_name(tmp_path: Path) -> None:
    """This is the property that actually matters: without it, any second
    install reuses the first one's containers and volumes."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert s05.resolve_compose_project_name(a) != s05.resolve_compose_project_name(b)


def test_project_name_reads_from_existing_env_first(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("COMPOSE_PROJECT_NAME=mi-proyecto\n", encoding="utf-8")
    assert s05.resolve_compose_project_name(tmp_path) == "mi-proyecto"


def test_project_name_migrates_the_legacy_literal_name_from_an_old_compose(
    tmp_path: Path,
) -> None:
    """Wizard versions before this fix wrote a literal `name: axion`, the same
    for everybody, into `docker-compose.yml`. Without migrating that value into
    `.env`, the first `install` after upgrading would generate a new name and
    lose access to the containers and volumes that already existed under the
    old one."""
    (tmp_path / "docker-compose.yml").write_text(
        "name: axion\nservices:\n  postgres:\n    image: postgres:15\n", encoding="utf-8"
    )
    assert s05.resolve_compose_project_name(tmp_path) == "axion"


def test_project_name_prefers_env_over_the_legacy_compose_literal(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yml").write_text(
        "name: axion\nservices: {}\n", encoding="utf-8"
    )
    (tmp_path / ".env").write_text("COMPOSE_PROJECT_NAME=already-migrated\n", encoding="utf-8")
    assert s05.resolve_compose_project_name(tmp_path) == "already-migrated"


def test_project_name_ignores_an_unparseable_legacy_compose(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yml").write_text("this: is not: valid: yaml:", encoding="utf-8")
    name = s05.resolve_compose_project_name(tmp_path)
    assert name.startswith(f"{s05.PROJECT_NAME_PREFIX}-")


def test_fresh_project_name_has_the_expected_prefix(tmp_path: Path) -> None:
    assert s05.resolve_compose_project_name(tmp_path).startswith(
        f"{s05.PROJECT_NAME_PREFIX}-"
    )


# --- render_env / render_wg_env / render_nginx_conf -------------------------


def test_render_env_contains_expected_values() -> None:
    config = make_config(ollama_model="llama3.1:8b")
    text = render_env(config)
    assert f"POSTGRES_PASSWORD={config.postgres_password.get_secret_value()}" in text
    assert "OLLAMA_MODEL=llama3.1:8b" in text
    assert "MM_SITEURL=https://192.168.1.50" in text


def test_render_env_has_an_empty_webhook_token_placeholder() -> None:
    """Mattermost generates this token when the outgoing webhook is created,
    after deployment — the wizard cannot fill it in beforehand. The key has to
    exist (empty) so anyone reading it knows it can be filled in, and so that
    `${MM_WEBHOOK_TOKEN:-}` in the compose file does not depend on the key
    being absent entirely rather than empty."""
    text = render_env(make_config())
    assert "MM_WEBHOOK_TOKEN=" in text


def test_render_compose_passes_the_webhook_token_through_to_fastapi() -> None:
    text = s05.render_compose(make_config())
    assert "MM_WEBHOOK_TOKEN" in text


def test_render_wg_env_contains_init_credentials_and_host() -> None:
    config = make_config(host="axion.example.com")
    text = s05.render_wg_env(config)
    assert "INIT_HOST=axion.example.com" in text
    assert "INIT_USERNAME=admin" in text
    assert f"INIT_PASSWORD={config.wireguard_admin_password.get_secret_value()}" in text
    # Without `INIT_ENABLED` the rest of the INIT_* variables do not apply
    # and the panel comes up in its web wizard waiting to be filled in.
    assert "INIT_ENABLED=true" in text


def test_render_wg_env_allows_plain_http() -> None:
    """Without `INSECURE=true`, wg-easy v15 refuses to serve over HTTP and the
    panel does not answer at all — and its URL is always built with http://."""
    assert "INSECURE=true" in s05.render_wg_env(make_config())


def test_render_wg_env_no_longer_writes_v14_keys() -> None:
    """`WG_HOST` and `PASSWORD_HASH` belong to v14, which v15 ignores
    silently. Leaving them assigned would suggest they configure something.

    Only assignment lines are inspected: the template's comments do name both
    keys, precisely to explain why they are no longer there.
    """
    assignments = [
        line for line in s05.render_wg_env(make_config()).splitlines()
        if line and not line.lstrip().startswith("#")
    ]
    keys = {line.split("=", 1)[0] for line in assignments if "=" in line}
    assert "PASSWORD_HASH" not in keys
    assert "WG_HOST" not in keys
    assert {"INIT_HOST", "INIT_USERNAME", "INIT_PASSWORD", "INIT_ENABLED"} <= keys


def test_render_nginx_conf_uses_host_as_server_name() -> None:
    config = make_config(host="axion.example.com")
    text = s05.render_nginx_conf(config)
    assert "server_name axion.example.com;" in text


def test_render_nginx_conf_only_upgrades_actual_websocket_requests() -> None:
    """A `Connection: upgrade` pinned for every request (not just the one
    genuinely asking for a WebSocket) stopped nginx reusing keepalive with the
    backend on ordinary HTTP requests."""
    text = s05.render_nginx_conf(make_config())
    assert "map $http_upgrade $connection_upgrade" in text
    assert "proxy_set_header Connection $connection_upgrade;" in text
    assert 'proxy_set_header Connection "upgrade";' not in text


def test_render_nginx_conf_sets_symmetric_websocket_timeouts() -> None:
    text = s05.render_nginx_conf(make_config())
    assert "proxy_read_timeout 600s;" in text
    assert "proxy_send_timeout 600s;" in text


def test_render_nginx_conf_re_resolves_the_backend_on_every_request() -> None:
    """With the name written literally in `proxy_pass`, nginx keeps the first
    IP it resolved: when the backup service stops and restarts Mattermost
    overnight, Docker gives it a different one and the whole stack is on 502 by
    morning without any healthcheck noticing."""
    text = s05.render_nginx_conf(make_config())

    # Docker's internal DNS; without `resolver`, nginx will not even start
    # with a variable in proxy_pass.
    assert "resolver 127.0.0.11" in text
    assert "set $mattermost_backend http://mattermost:8065;" in text
    # The variable is what forces re-resolution…
    assert "proxy_pass $mattermost_backend$request_uri;" in text
    # …and `$request_uri` is what stops the path being lost along the way.
    assert "proxy_pass http://mattermost" not in text


def test_render_nginx_conf_has_no_static_upstream_block() -> None:
    """An `upstream` block with a name inside resolves exactly once, at
    configuration load: that is precisely what had to go."""
    assert "upstream mattermost_backend" not in s05.render_nginx_conf(make_config())


# --- validaciones ------------------------------------------------------------


def test_validate_compose_yaml_shape_rejects_missing_services() -> None:
    with pytest.raises(ConfigError, match="not shaped as expected"):
        s05.validate_compose_yaml_shape("not_services: {}\n")


def test_validate_compose_yaml_shape_rejects_missing_managed_service() -> None:
    with pytest.raises(ConfigError, match="Managed services missing"):
        s05.validate_compose_yaml_shape("services:\n  postgres: {}\n")


def test_validate_compose_yaml_shape_accepts_rendered_output() -> None:
    s05.validate_compose_yaml_shape(s05.render_compose(make_config()))  # must not raise


def test_assert_ssrf_env_present_raises_when_missing() -> None:
    with pytest.raises(ConfigError, match="SSRF"):
        s05.assert_ssrf_env_present("services: {}\n")


def test_assert_ssrf_env_present_accepts_rendered_output() -> None:
    s05.assert_ssrf_env_present(s05.render_compose(make_config()))  # must not raise


def test_assert_no_unpinned_images_rejects_latest() -> None:
    with pytest.raises(ConfigError, match="latest"):
        s05.assert_no_unpinned_images("image: something:latest\n")


def test_assert_no_unpinned_images_accepts_rendered_output() -> None:
    s05.assert_no_unpinned_images(s05.render_compose(make_config()))  # must not raise


@functools.cache
def _docker_compose_is_usable() -> bool:
    """`True` only if `docker compose` genuinely answers.

    `shutil.which("docker")` is not enough, which is what was there before: in
    a WSL distro without Docker Desktop's integration enabled there is a
    `docker` shim on the PATH that always fails with "The command 'docker'
    could not be found in this WSL 2 distro". The test ran anyway and failed
    because of the environment, blaming the generated compose file.
    """
    if shutil.which("docker") is None:
        return False
    try:
        completed = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


@pytest.mark.skipif(
    not _docker_compose_is_usable(),
    reason="`docker compose` is unavailable or does not answer on this machine",
)
@pytest.mark.parametrize("variant", [WireguardVariant.HOST, WireguardVariant.PORTS])
def test_render_compose_passes_real_docker_compose_config(tmp_path: Path, variant) -> None:
    """Shape validation cannot see indentation errors: a badly closed block
    can push a service out of `services:` and still be perfectly valid YAML.
    Only Compose catches it."""
    config = make_config(wireguard_variant=variant)
    compose_path = tmp_path / "docker-compose.yml"
    compose_path.write_text(s05.render_compose(config), encoding="utf-8")
    (tmp_path / ".env").write_text(
        render_env(config) + "\nPOSTGRES_PASSWORD=x\n", encoding="utf-8"
    )
    (tmp_path / "fastapi").mkdir()
    (tmp_path / "fastapi" / "Dockerfile").write_text("FROM python:3.12-slim\n")
    (tmp_path / "wg.env").write_text("WG_HOST=x\n")

    compose_service.config_validate(compose_path)  # must not raise


# --- backup / merge onto an already existing compose file -------------------


def test_backup_existing_file_returns_none_when_absent(tmp_path: Path) -> None:
    assert s05.backup_existing_file(tmp_path / "nope.yml") is None


def test_backup_existing_file_copies_content_with_timestamp_suffix(tmp_path: Path) -> None:
    target = tmp_path / "docker-compose.yml"
    target.write_text("original: true\n")
    backup = s05.backup_existing_file(target)
    assert backup is not None
    assert backup.name.startswith("docker-compose.yml.")
    assert backup.name.endswith(".bak")
    assert backup.read_text() == "original: true\n"


def test_backup_existing_file_does_not_clobber_a_backup_from_the_same_second(
    tmp_path: Path,
) -> None:
    """Regression: the timestamp has one-second resolution, so two backups in
    a row collided and the second overwrote the first — the very copy this
    mechanism exists to keep."""
    target = tmp_path / "docker-compose.yml"

    target.write_text("version: ONE\n")
    first = s05.backup_existing_file(target)
    target.write_text("version: TWO\n")
    second = s05.backup_existing_file(target)

    assert first is not None and second is not None
    assert first != second, "the second backup reused the first one's name"
    assert first.read_text() == "version: ONE\n"
    assert second.read_text() == "version: TWO\n"


def test_backup_existing_file_handles_many_backups_in_the_same_second(tmp_path: Path) -> None:
    target = tmp_path / "docker-compose.yml"
    paths = []
    for i in range(5):
        target.write_text(f"version: {i}\n")
        backup = s05.backup_existing_file(target)
        assert backup is not None
        paths.append(backup)

    assert len({p.name for p in paths}) == 5
    for i, path in enumerate(paths):
        assert path.read_text() == f"version: {i}\n"


def test_merge_compose_preserves_user_added_service() -> None:
    existing = (
        "services:\n"
        "  postgres:\n"
        "    image: postgres:13-alpine  # the user's old version\n"
        "  custom-tool:\n"
        "    image: myorg/custom-tool:1.0\n"
    )
    rendered = s05.render_compose(make_config())
    merged = s05.merge_compose_preserving_user_edits(existing, rendered)
    data = _load_yaml(merged)
    assert data["services"]["custom-tool"]["image"] == "myorg/custom-tool:1.0"
    assert data["services"]["postgres"]["image"] == images.POSTGRES_IMAGE


def test_merge_compose_does_not_touch_a_legacy_name_key() -> None:
    """The merge no longer manages `name` — it lives in `.env`. A
    `docker-compose.yml` from an earlier version that carried it as a literal
    keeps it intact: harmless (`COMPOSE_PROJECT_NAME` in `.env` overrides the
    compose file's own `name:`), and it is exactly what allows migrating it on
    the first render (see `resolve_compose_project_name`)."""
    existing = "name: axion\nservices:\n  postgres:\n    image: postgres:13-alpine\n"
    merged = s05.merge_compose_preserving_user_edits(existing, s05.render_compose(make_config()))
    assert _load_yaml(merged)["name"] == "axion"


def test_merge_compose_rejects_non_mapping_root_with_actionable_error() -> None:
    """Regression: a compose file whose root is not a mapping blew the merge up
    with a raw `TypeError` instead of an actionable error."""
    rendered = s05.render_compose(make_config())
    with pytest.raises(ConfigError, match="no mapping at its root"):
        s05.merge_compose_preserving_user_edits("- just\n- a\n- list\n", rendered)


def test_merge_compose_rejects_invalid_yaml_with_actionable_error() -> None:
    rendered = s05.render_compose(make_config())
    with pytest.raises(ConfigError, match="not valid YAML"):
        s05.merge_compose_preserving_user_edits("services: [unclosed\n", rendered)


def test_merge_compose_handles_empty_existing_file() -> None:
    rendered = s05.render_compose(make_config())
    merged = s05.merge_compose_preserving_user_edits("", rendered)
    data = _load_yaml(merged)
    assert data["services"]["postgres"]["image"] == images.POSTGRES_IMAGE


def test_merge_compose_handles_services_key_present_but_null() -> None:
    rendered = s05.render_compose(make_config())
    merged = s05.merge_compose_preserving_user_edits("services:\n", rendered)
    data = _load_yaml(merged)
    for name in MANAGED_SERVICES:
        assert name in data["services"]


def test_merge_compose_handles_services_key_of_wrong_type() -> None:
    rendered = s05.render_compose(make_config())
    merged = s05.merge_compose_preserving_user_edits("services: not-a-mapping\n", rendered)
    data = _load_yaml(merged)
    assert data["services"]["postgres"]["image"] == images.POSTGRES_IMAGE


def test_merge_compose_preserves_comments() -> None:
    existing = "# an important comment from the user\nservices:\n  postgres:\n    image: old\n"
    rendered = s05.render_compose(make_config())
    merged = s05.merge_compose_preserving_user_edits(existing, rendered)
    assert "# an important comment from the user" in merged


def test_render_compose_to_disk_writes_new_file_without_backup(tmp_path: Path) -> None:
    compose_path = tmp_path / "docker-compose.yml"
    backup = s05.render_compose_to_disk(make_config(), compose_path)
    assert backup is None
    assert compose_path.exists()


def test_render_compose_to_disk_backs_up_and_merges_existing_file(tmp_path: Path) -> None:
    compose_path = tmp_path / "docker-compose.yml"
    compose_path.write_text(
        "services:\n  postgres:\n    image: old\n  custom-tool:\n    image: keep-me:1.0\n"
    )
    backup = s05.render_compose_to_disk(make_config(), compose_path)
    assert backup is not None
    assert backup.exists()

    data = _load_yaml(compose_path.read_text(encoding="utf-8"))
    assert data["services"]["custom-tool"]["image"] == "keep-me:1.0"
    assert data["services"]["postgres"]["image"] == images.POSTGRES_IMAGE


# --- write_secret_env_file ----------------------------------------------------


def test_write_secret_env_file_writes_and_restricts(mocker, tmp_path: Path) -> None:
    restrict_mock = mocker.patch("axion_wizard.steps.s05_compose.restrict_to_owner")
    target = tmp_path / ".env"
    s05.write_secret_env_file(target, "SECRET=1\n")
    assert target.read_text() == "SECRET=1\n"
    restrict_mock.assert_called_once_with(target)


# --- update_env_value ----------------------------------------------------------


def test_update_env_value_replaces_an_existing_key(mocker, tmp_path: Path) -> None:
    mocker.patch("axion_wizard.steps.s05_compose.restrict_to_owner")
    target = tmp_path / ".env"
    target.write_text("POSTGRES_PASSWORD=abc\nMM_WEBHOOK_TOKEN=\nOLLAMA_MODEL=x\n")

    s05.update_env_value(target, "MM_WEBHOOK_TOKEN", "newtoken123")

    lines = target.read_text().splitlines()
    assert lines == ["POSTGRES_PASSWORD=abc", "MM_WEBHOOK_TOKEN=newtoken123", "OLLAMA_MODEL=x"]


def test_update_env_value_appends_a_missing_key(mocker, tmp_path: Path) -> None:
    mocker.patch("axion_wizard.steps.s05_compose.restrict_to_owner")
    target = tmp_path / ".env"
    target.write_text("POSTGRES_PASSWORD=abc\n")

    s05.update_env_value(target, "MM_WEBHOOK_TOKEN", "newtoken123")

    lines = target.read_text().splitlines()
    assert lines == ["POSTGRES_PASSWORD=abc", "MM_WEBHOOK_TOKEN=newtoken123"]


def test_update_env_value_creates_the_file_if_missing(mocker, tmp_path: Path) -> None:
    mocker.patch("axion_wizard.steps.s05_compose.restrict_to_owner")
    target = tmp_path / ".env"

    s05.update_env_value(target, "MM_WEBHOOK_TOKEN", "newtoken123")

    assert target.read_text() == "MM_WEBHOOK_TOKEN=newtoken123\n"


def test_update_env_value_preserves_comments_and_order(mocker, tmp_path: Path) -> None:
    mocker.patch("axion_wizard.steps.s05_compose.restrict_to_owner")
    target = tmp_path / ".env"
    target.write_text("# comentario\nA=1\nMM_WEBHOOK_TOKEN=old\nB=2\n")

    s05.update_env_value(target, "MM_WEBHOOK_TOKEN", "new")

    lines = target.read_text().splitlines()
    assert lines == ["# comentario", "A=1", "MM_WEBHOOK_TOKEN=new", "B=2"]


def test_update_env_value_restricts_permissions(mocker, tmp_path: Path) -> None:
    restrict_mock = mocker.patch("axion_wizard.steps.s05_compose.restrict_to_owner")
    target = tmp_path / ".env"

    s05.update_env_value(target, "MM_WEBHOOK_TOKEN", "newtoken123")

    restrict_mock.assert_called_once_with(target)


# --- .gitignore ---------------------------------------------------------------


def test_ensure_gitignore_entries_noop_without_git_repo(tmp_path: Path) -> None:
    assert s05.ensure_gitignore_entries(tmp_path) is False
    assert not (tmp_path / ".gitignore").exists()


def test_ensure_gitignore_entries_creates_file_when_repo_present(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    changed = s05.ensure_gitignore_entries(tmp_path)
    assert changed is True
    content = (tmp_path / ".gitignore").read_text()
    for entry in [".env", "wg.env", "nginx/certs/", ".axion-wizard-state.json", "backups/"]:
        assert entry in content


def test_ensure_gitignore_entries_does_not_duplicate(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text(
        ".env\nwg.env\nnginx/certs/\n.axion-wizard-state.json\nbackups/\n"
    )
    changed = s05.ensure_gitignore_entries(tmp_path)
    assert changed is False


def test_ensure_gitignore_entries_appends_only_missing(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("node_modules/\n.env\n")
    changed = s05.ensure_gitignore_entries(tmp_path)
    assert changed is True
    lines = (tmp_path / ".gitignore").read_text().splitlines()
    assert lines.count(".env") == 1
    assert "wg.env" in lines


# --- Zone.Identifier cleanup --------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="On NTFS 'file:Zone.Identifier' is an alternate data stream, not a "
    "listable file — the real case only happens on WSL/Linux, where ':' is a "
    "literal filename character.",
)
def test_clean_zone_identifier_files_removes_matches(tmp_path: Path) -> None:
    zone_file = tmp_path / "docker-compose.yml:Zone.Identifier"
    zone_file.write_text("[ZoneTransfer]\nZoneId=3\n")
    normal_file = tmp_path / "docker-compose.yml"
    normal_file.write_text("services: {}\n")

    removed, failed = s05.clean_zone_identifier_files(tmp_path)

    assert removed == [zone_file]
    assert failed == []
    assert not zone_file.exists()
    assert normal_file.exists()


def test_clean_zone_identifier_files_empty_when_none_present(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    assert s05.clean_zone_identifier_files(tmp_path) == ([], [])


def test_clean_zone_identifier_files_survives_an_unremovable_file(
    tmp_path: Path, mocker
) -> None:
    """Regression: the `OSError` from a locked file escaped uncaught and took
    down the whole of step 5 — the one that writes the compose file, .env and
    the certificate — surfacing as `Unexpected error`. It is cosmetic litter:
    it is reported and the step carries on.

    The walk is simulated rather than creating the file because on NTFS
    `a.txt:Zone.Identifier` is an alternate data stream and does not show up as
    a listable file; what is under test here is the error handling, which does
    not depend on the filesystem.
    """
    mocker.patch(
        "axion_wizard.steps.s05_compose.os.walk",
        return_value=[(str(tmp_path), [], ["a.txt:Zone.Identifier"])],
    )
    mocker.patch.object(Path, "unlink", side_effect=OSError("in use by another process"))

    removed, failed = s05.clean_zone_identifier_files(tmp_path)

    assert removed == []
    assert failed == [tmp_path / "a.txt:Zone.Identifier"]


def test_clean_zone_identifier_does_not_descend_into_heavy_directories(
    tmp_path: Path, mocker
) -> None:
    """`.venv` and `.git` hold tens of thousands of files and cannot contain a
    Zone.Identifier that matters: walking them turned an instant cleanup into a
    pause of several seconds."""
    walked_dirnames: list[str] = ["fastapi", ".venv", ".git", "node_modules", "nginx"]
    mocker.patch(
        "axion_wizard.steps.s05_compose.os.walk",
        return_value=[(str(tmp_path), walked_dirnames, [])],
    )

    s05.clean_zone_identifier_files(tmp_path)

    # `os.walk` honours pruning `dirnames` in place.
    assert walked_dirnames == ["fastapi", "nginx"]


# --- values that survive a second `install` -------------------------------------
#
# Regression from a silent data loss: `.env` was regenerated in full on every
# run, so a second `install` — to change the model, say — wiped the webhook
# token. fastapi went back to accepting any call without validating it, with no
# error, no warning and nothing in the logs.


def test_render_env_preserves_the_webhook_token() -> None:
    text = render_env(make_config(), preserved={"MM_WEBHOOK_TOKEN": "tok3n-real"})
    assert "MM_WEBHOOK_TOKEN=tok3n-real" in text


def test_render_env_preserves_the_system_prompt() -> None:
    text = render_env(
        make_config(), preserved={"OLLAMA_SYSTEM_PROMPT": "Always answer in English."}
    )
    assert "OLLAMA_SYSTEM_PROMPT=Always answer in English." in text


def test_render_env_defaults_to_empty_when_nothing_to_preserve() -> None:
    text = render_env(make_config())
    assert "MM_WEBHOOK_TOKEN=\n" in text
    assert "OLLAMA_SYSTEM_PROMPT=\n" in text


def test_preserved_env_values_reads_the_existing_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "POSTGRES_PASSWORD=abc\nMM_WEBHOOK_TOKEN=tok3n\nOLLAMA_SYSTEM_PROMPT=be brief\n",
        encoding="utf-8",
    )
    preserved = s05.preserved_env_values(tmp_path)
    assert preserved["MM_WEBHOOK_TOKEN"] == "tok3n"
    assert preserved["OLLAMA_SYSTEM_PROMPT"] == "be brief"


def test_preserved_env_values_empty_without_a_previous_env(tmp_path: Path) -> None:
    assert s05.preserved_env_values(tmp_path) == dict.fromkeys(s05.PRESERVED_ENV_KEYS, "")


def test_a_second_render_round_trip_keeps_the_token(tmp_path: Path) -> None:
    """The full cycle: `set-webhook-token` writes, `install` regenerates."""
    env_path = tmp_path / ".env"
    s05.write_secret_env_file(env_path, render_env(make_config()))
    s05.update_env_value(env_path, "MM_WEBHOOK_TOKEN", "example-token-not-real-000")

    preserved = s05.preserved_env_values(tmp_path)
    s05.write_secret_env_file(env_path, render_env(make_config(), preserved=preserved))

    assert "MM_WEBHOOK_TOKEN=example-token-not-real-000" in env_path.read_text(encoding="utf-8")


def test_write_secret_env_file_backs_up_the_previous_version(tmp_path: Path) -> None:
    """`.env` and `wg.env` carry secrets and are regenerated whole; they were
    the only managed files without a backup."""
    env_path = tmp_path / ".env"
    env_path.write_text("POSTGRES_PASSWORD=old\n", encoding="utf-8")

    backup = s05.write_secret_env_file(env_path, "POSTGRES_PASSWORD=new\n", backup=True)

    assert backup is not None
    assert backup.read_text(encoding="utf-8") == "POSTGRES_PASSWORD=old\n"
    assert env_path.read_text(encoding="utf-8") == "POSTGRES_PASSWORD=new\n"


def test_write_secret_env_file_without_a_previous_version_has_no_backup(tmp_path: Path) -> None:
    assert s05.write_secret_env_file(tmp_path / ".env", "A=1\n", backup=True) is None


def test_existing_env_value_survives_a_file_it_cannot_decode(tmp_path: Path) -> None:
    """`OLLAMA_SYSTEM_PROMPT` invites editing `.env` by hand, and a Windows
    editor saving in ANSI produces bytes that are not UTF-8. Losing that value is
    bad; aborting the whole install over it is worse."""
    (tmp_path / ".env").write_bytes(b"OLLAMA_SYSTEM_PROMPT=answer in espa\xf1ol\n")
    assert s05.existing_env_value(tmp_path, "OLLAMA_SYSTEM_PROMPT") is None
    assert s05.preserved_env_values(tmp_path) == dict.fromkeys(s05.PRESERVED_ENV_KEYS, "")


# --- the panel password must survive Compose interpolation ------------------------
#
# Regression from a mute failure, confirmed live against wg-easy v14: `wg.env`
# held `PASSWORD_HASH=$2b$12$GktCd...` and what reached the container was
# `$2b$12.96FL219a`. Docker Compose DOES interpolate the values in `env_file:`
# — contrary to what the template's comment claimed — and ate `$GktCd...` as an
# undefined variable. The panel came up healthy and rejected every password,
# writing nothing at all to its logs.
#
# v15 takes the password in plaintext, so the hash and its escaping are gone.
# The hazard is not: any `$` in the password would be interpolated just the
# same. What prevents it now is that `$` is forbidden in it, and that is what
# these tests hold up.


def test_the_panel_password_cannot_contain_an_interpolable_dollar() -> None:
    with pytest.raises(ValueError, match=r"\$"):
        make_config(wireguard_admin_password="has$a-dollar-and-is-long-enough")


def test_wg_env_password_survives_a_compose_interpolation_round_trip() -> None:
    """What Compose would hand the container is exactly what was written.

    With no `$` there is nothing to expand, which is precisely the property the
    password validation guarantees — hence nothing needs escaping in the
    template any more.
    """
    config = make_config()
    raw = config.wireguard_admin_password.get_secret_value()
    line = next(
        line
        for line in s05.render_wg_env(config).splitlines()
        if line.startswith("INIT_PASSWORD=")
    )

    assert line.split("=", 1)[1] == raw
    assert "$" not in raw


def test_wg_host_is_not_mangled() -> None:
    text = s05.render_wg_env(make_config(host="192.168.1.50"))
    assert "INIT_HOST=192.168.1.50" in text
