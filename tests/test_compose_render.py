import functools
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from axion_wizard import images
from axion_wizard.config import AccessMode, AxionConfig, WireguardVariant
from axion_wizard.errors import ConfigError
from axion_wizard.services import compose as compose_service
from axion_wizard.steps import s05_compose as s05
from axion_wizard.utils.secrets import generate_hex_secret, hash_password


def make_config(**overrides) -> AxionConfig:
    kwargs = dict(
        access_mode=AccessMode.LAN,
        host="192.168.1.50",
        wireguard_variant=WireguardVariant.PORTS,
        postgres_password=generate_hex_secret(),
        wireguard_admin_password_hash=hash_password("correct-horse-battery-staple"),
        ollama_model="qwen2.5:1.5b",
        project_dir=Path("."),
    )
    kwargs.update(overrides)
    return AxionConfig(**kwargs)


def _load_yaml(text: str) -> dict:
    return YAML(typ="safe").load(text)


# --- render_compose: forma general -----------------------------------------


def test_render_compose_is_valid_yaml_with_managed_services() -> None:
    data = _load_yaml(s05.render_compose(make_config()))
    for name in s05.MANAGED_SERVICES:
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
    """Los 30 s por defecto de Mattermost no le llegan a un modelo en CPU: al
    agotarse, la respuesta se pierde entera y no aparece error en ningún log."""
    mattermost = _load_yaml(s05.render_compose(make_config()))["services"]["mattermost"]
    timeout = mattermost["environment"]["MM_SERVICESETTINGS_OUTGOINGINTEGRATIONREQUESTSTIMEOUT"]

    assert int(timeout) >= 120, "un margen menor deja fuera a los modelos de 3B en CPU"


def test_render_compose_injects_ssrf_env_var() -> None:
    text = s05.render_compose(make_config())
    assert f'{s05.SSRF_ENV_KEY}: "{s05.SSRF_ENV_VALUE}"' in text


# --- n8n (opcional) ----------------------------------------------------------


def test_n8n_is_absent_unless_asked_for() -> None:
    data = _load_yaml(s05.render_compose(make_config()))
    assert "n8n" not in data["services"]
    assert "n8n_data" not in data["volumes"]


def test_n8n_is_rendered_with_a_pinned_image_and_its_volume() -> None:
    data = _load_yaml(s05.render_compose(make_config(), with_n8n=True))
    n8n = data["services"]["n8n"]

    assert n8n["image"] == images.N8N_IMAGE
    assert ":latest" not in n8n["image"]
    assert "n8n_data" in data["volumes"]
    assert "5678:5678" in n8n["ports"]


def test_n8n_is_announced_over_http_because_nothing_terminates_tls_for_it() -> None:
    """n8n se publica en su propio puerto, sin pasar por nginx. Anunciarse
    como https haría que generase URLs de webhook que no responden."""
    n8n = _load_yaml(s05.render_compose(make_config(), with_n8n=True))["services"]["n8n"]

    assert n8n["environment"]["N8N_PROTOCOL"] == "http"
    assert n8n["environment"]["WEBHOOK_URL"].startswith("http://")


def test_mattermost_is_allowed_to_reach_n8n() -> None:
    """La protección SSRF de Mattermost descarta el webhook saliente en
    silencio: no dispara y no aparece error en ningún log."""
    assert "n8n:5678" in s05.SSRF_ENV_VALUE
    assert s05.SSRF_ENV_VALUE in s05.render_compose(make_config(), with_n8n=True)


def test_backup_covers_n8n_when_it_is_installed() -> None:
    """Sin esto, una restauración devolvería el chat entero y n8n vacío."""
    services = _load_yaml(s05.render_compose(make_config(), with_n8n=True))["services"]
    sources = {entry.split(":")[0] for entry in services["backup"]["volumes"]}

    assert "n8n_data" in sources


def test_n8n_encryption_key_is_generated_once_and_then_preserved() -> None:
    """Si esa clave cambia, las credenciales guardadas en n8n quedan
    ilegibles para siempre y nada avisa hasta que un flujo falla."""
    first = s05.render_env(make_config())
    prefix = "N8N_ENCRYPTION_KEY="
    key = next(line[len(prefix) :] for line in first.splitlines() if line.startswith(prefix))
    assert key

    again = s05.render_env(make_config(), preserved={"N8N_ENCRYPTION_KEY": key})
    assert f"N8N_ENCRYPTION_KEY={key}" in again


def test_managed_services_follow_the_compose_on_disk() -> None:
    """`up` y `doctor` no reciben el flag: deducen del compose si el
    despliegue lleva n8n."""
    sin_n8n = s05.render_compose(make_config())
    con_n8n = s05.render_compose(make_config(), with_n8n=True)

    assert "n8n" not in s05.managed_services_in(sin_n8n)
    assert "n8n" in s05.managed_services_in(con_n8n)


def test_managed_services_falls_back_to_the_base_list_on_a_broken_compose() -> None:
    assert s05.managed_services_in("esto: no es: yaml: valido:") == s05.MANAGED_SERVICES


def test_merge_regenerates_n8n_but_never_deletes_it() -> None:
    """`--with-n8n` es aditivo: olvidarlo en un `install` posterior no puede
    borrar el servicio y dejar su volumen huérfano."""
    existing = s05.render_compose(make_config(), with_n8n=True)
    sin_flag = s05.render_compose(make_config(), with_n8n=False)

    merged = _load_yaml(s05.merge_compose_preserving_user_edits(existing, sin_flag))
    assert "n8n" in merged["services"]


# --- servicio de copias de seguridad ----------------------------------------


def test_backup_service_archives_the_volumes_that_matter_read_only() -> None:
    backup = _load_yaml(s05.render_compose(make_config()))["services"]["backup"]
    mounted = {entry.split(":")[0]: entry for entry in backup["volumes"]}

    for volume in (
        "postgres_data",
        "mattermost_data",
        "mattermost_config",
        "wireguard_data",
    ):
        assert volume in mounted, f"{volume} no se está copiando"
        assert mounted[volume].endswith(":ro"), f"{volume} debe montarse de solo lectura"


def test_backup_service_excludes_the_model_and_the_logs() -> None:
    """`ollama_data` son gigabytes que se vuelven a descargar y los logs no se
    restauran: incluirlos multiplicaría el tamaño de cada copia a cambio de
    nada."""
    backup = _load_yaml(s05.render_compose(make_config()))["services"]["backup"]
    sources = {entry.split(":")[0] for entry in backup["volumes"]}

    assert "ollama_data" not in sources
    assert "mattermost_logs" not in sources


def test_backup_stops_postgres_and_mattermost_with_a_matching_label() -> None:
    """La etiqueta de los contenedores y el valor que busca el servicio tienen
    que coincidir exactamente: si no, no se para nada y la copia de PostgreSQL
    se hace en caliente sin un solo aviso."""
    services = _load_yaml(s05.render_compose(make_config()))["services"]
    expected = services["backup"]["environment"]["BACKUP_STOP_DURING_BACKUP_LABEL"]

    for name in ("postgres", "mattermost"):
        assert services[name]["labels"]["docker-volume-backup.stop-during-backup"] == expected


def test_backup_prunes_only_its_own_archives() -> None:
    """Sin prefijo, el borrado por antigüedad alcanzaría a cualquier archivo
    del destino."""
    backup = _load_yaml(s05.render_compose(make_config()))["services"]["backup"]
    prefix = backup["environment"]["BACKUP_PRUNING_PREFIX"]

    assert prefix
    assert backup["environment"]["BACKUP_FILENAME"].startswith(prefix)


def test_env_gets_backup_defaults_on_a_fresh_install() -> None:
    text = s05.render_env(make_config())
    assert f"BACKUP_CRON_EXPRESSION={s05.DEFAULT_BACKUP_CRON_EXPRESSION}" in text
    assert f"BACKUP_RETENTION_DAYS={s05.DEFAULT_BACKUP_RETENTION_DAYS}" in text


def test_env_keeps_a_customised_backup_schedule() -> None:
    text = s05.render_env(
        make_config(), preserved={"BACKUP_CRON_EXPRESSION": "30 4 * * 0"}
    )
    assert "BACKUP_CRON_EXPRESSION=30 4 * * 0" in text


def test_env_falls_back_to_defaults_when_the_previous_value_is_empty() -> None:
    """Un valor vacío se interpolaría tal cual y dejaría al servicio sin
    horario ni retención — a diferencia de los tokens, aquí el vacío no es un
    valor válido."""
    text = s05.render_env(
        make_config(), preserved={"BACKUP_CRON_EXPRESSION": "", "BACKUP_RETENTION_DAYS": ""}
    )
    assert f"BACKUP_CRON_EXPRESSION={s05.DEFAULT_BACKUP_CRON_EXPRESSION}" in text
    assert f"BACKUP_RETENTION_DAYS={s05.DEFAULT_BACKUP_RETENTION_DAYS}" in text


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
    """Los dos a la vez o no sirve de nada: la imagen por defecto ignora
    `/dev/kfd`, y la de ROCm sin los dispositivos no tiene qué usar."""
    data = _load_yaml(s05.render_compose(make_config(), gpu_acceleration="rocm"))
    ollama = data["services"]["ollama"]

    assert ollama["image"] == images.OLLAMA_ROCM_IMAGE
    assert ollama["devices"] == ["/dev/kfd", "/dev/dri"]
    assert ollama["group_add"] == ["video", "render"]
    assert "deploy" not in ollama


def test_render_compose_fixes_the_project_name() -> None:
    """Sin `name`, Compose lo deduce de la carpeta y prefija los volúmenes con
    él: mover el directorio estrenaba volúmenes vacíos en silencio."""
    data = _load_yaml(s05.render_compose(make_config()))
    assert data["name"] == s05.PROJECT_NAME


# --- render_env / render_wg_env / render_nginx_conf -------------------------


def test_render_env_contains_expected_values() -> None:
    config = make_config(ollama_model="llama3.1:8b")
    text = s05.render_env(config)
    assert f"POSTGRES_PASSWORD={config.postgres_password.get_secret_value()}" in text
    assert "OLLAMA_MODEL=llama3.1:8b" in text
    assert "MM_SITEURL=https://192.168.1.50" in text


def test_render_env_has_an_empty_webhook_token_placeholder() -> None:
    """Mattermost genera este token al crear el webhook saliente, después
    del despliegue — el wizard no puede rellenarlo de antemano. Debe existir
    la clave (vacía) para que quien la lea sepa que se puede rellenar, y
    para que `${MM_WEBHOOK_TOKEN:-}` en el compose no dependa de que la
    clave falte por completo en vez de estar vacía."""
    text = s05.render_env(make_config())
    assert "MM_WEBHOOK_TOKEN=" in text


def test_render_compose_passes_the_webhook_token_through_to_fastapi() -> None:
    text = s05.render_compose(make_config())
    assert "MM_WEBHOOK_TOKEN" in text


def test_render_wg_env_contains_bcrypt_hash_and_host() -> None:
    config = make_config(host="axion.example.com")
    text = s05.render_wg_env(config)
    assert "WG_HOST=axion.example.com" in text
    # El hash va con los `$` escapados como `$$`: Compose interpola los
    # valores de `env_file:` y sin escapar llega destrozado al contenedor.
    escaped = config.wireguard_admin_password_hash.get_secret_value().replace("$", "$$")
    assert f"PASSWORD_HASH={escaped}" in text


def test_render_nginx_conf_uses_host_as_server_name() -> None:
    config = make_config(host="axion.example.com")
    text = s05.render_nginx_conf(config)
    assert "server_name axion.example.com;" in text


def test_render_nginx_conf_only_upgrades_actual_websocket_requests() -> None:
    """`Connection: upgrade` fijo para toda petición (no solo la que de
    verdad pide WebSocket) le impedía a nginx reusar keepalive con el
    backend en peticiones HTTP normales."""
    text = s05.render_nginx_conf(make_config())
    assert "map $http_upgrade $connection_upgrade" in text
    assert "proxy_set_header Connection $connection_upgrade;" in text
    assert 'proxy_set_header Connection "upgrade";' not in text


def test_render_nginx_conf_sets_symmetric_websocket_timeouts() -> None:
    text = s05.render_nginx_conf(make_config())
    assert "proxy_read_timeout 600s;" in text
    assert "proxy_send_timeout 600s;" in text


def test_render_nginx_conf_re_resolves_the_backend_on_every_request() -> None:
    """Con el nombre escrito literalmente en `proxy_pass`, nginx se queda con
    la primera IP que resolvió: cuando el servicio de copias para y rearranca
    Mattermost de madrugada, Docker le da otra y el stack entero amanece en
    502 sin que ningún healthcheck lo note."""
    text = s05.render_nginx_conf(make_config())

    # El DNS interno de Docker; sin `resolver` nginx ni siquiera arranca con
    # una variable en proxy_pass.
    assert "resolver 127.0.0.11" in text
    assert "set $mattermost_backend http://mattermost:8065;" in text
    # La variable es lo que fuerza la re-resolución...
    assert "proxy_pass $mattermost_backend$request_uri;" in text
    # ...y `$request_uri` lo que evita perder la ruta por el camino.
    assert "proxy_pass http://mattermost" not in text


def test_render_nginx_conf_has_no_static_upstream_block() -> None:
    """Un bloque `upstream` con un nombre dentro resuelve una sola vez, al
    cargar la configuración: es justo lo que había que quitar."""
    assert "upstream mattermost_backend" not in s05.render_nginx_conf(make_config())


# --- validaciones ------------------------------------------------------------


def test_validate_compose_yaml_shape_rejects_missing_services() -> None:
    with pytest.raises(ConfigError, match="forma esperada"):
        s05.validate_compose_yaml_shape("not_services: {}\n")


def test_validate_compose_yaml_shape_rejects_missing_managed_service() -> None:
    with pytest.raises(ConfigError, match="Faltan servicios"):
        s05.validate_compose_yaml_shape("services:\n  postgres: {}\n")


def test_validate_compose_yaml_shape_accepts_rendered_output() -> None:
    s05.validate_compose_yaml_shape(s05.render_compose(make_config()))  # no debe lanzar


def test_assert_ssrf_env_present_raises_when_missing() -> None:
    with pytest.raises(ConfigError, match="SSRF"):
        s05.assert_ssrf_env_present("services: {}\n")


def test_assert_ssrf_env_present_accepts_rendered_output() -> None:
    s05.assert_ssrf_env_present(s05.render_compose(make_config()))  # no debe lanzar


def test_assert_no_unpinned_images_rejects_latest() -> None:
    with pytest.raises(ConfigError, match="latest"):
        s05.assert_no_unpinned_images("image: something:latest\n")


def test_assert_no_unpinned_images_accepts_rendered_output() -> None:
    s05.assert_no_unpinned_images(s05.render_compose(make_config()))  # no debe lanzar


@functools.cache
def _docker_compose_is_usable() -> bool:
    """`True` solo si `docker compose` responde de verdad.

    No basta con `shutil.which("docker")`, que es lo que había antes: en una
    distro de WSL sin la integración de Docker Desktop activada existe un
    shim `docker` en el PATH que siempre falla con "The command 'docker'
    could not be found in this WSL 2 distro". El test se ejecutaba igual y
    fallaba por el entorno, culpando al compose generado.
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
    reason="`docker compose` no está disponible o no responde en esta máquina",
)
@pytest.mark.parametrize("variant", [WireguardVariant.HOST, WireguardVariant.PORTS])
@pytest.mark.parametrize("with_n8n", [False, True], ids=["sin-n8n", "con-n8n"])
def test_render_compose_passes_real_docker_compose_config(
    tmp_path: Path, variant, with_n8n: bool
) -> None:
    """La validación de forma no ve los errores de sangría: un bloque
    condicional mal cerrado puede sacar un servicio fuera de `services:` y
    seguir siendo YAML perfectamente válido. Solo Compose lo detecta."""
    config = make_config(wireguard_variant=variant)
    compose_path = tmp_path / "docker-compose.yml"
    compose_path.write_text(s05.render_compose(config, with_n8n=with_n8n), encoding="utf-8")
    (tmp_path / ".env").write_text(
        s05.render_env(config) + "\nPOSTGRES_PASSWORD=x\n", encoding="utf-8"
    )
    (tmp_path / "fastapi").mkdir()
    (tmp_path / "fastapi" / "Dockerfile").write_text("FROM python:3.12-slim\n")
    (tmp_path / "wg.env").write_text("WG_HOST=x\n")

    compose_service.config_validate(compose_path)  # no debe lanzar


# --- backup / merge sobre compose ya existente ------------------------------


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
    """Regresión: el timestamp tiene resolución de segundo, así que dos
    backups seguidos colisionaban y el segundo pisaba al primero — la copia
    que este mecanismo existe justamente para conservar."""
    target = tmp_path / "docker-compose.yml"

    target.write_text("version: ONE\n")
    first = s05.backup_existing_file(target)
    target.write_text("version: TWO\n")
    second = s05.backup_existing_file(target)

    assert first is not None and second is not None
    assert first != second, "el segundo backup reutilizó el nombre del primero"
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
        "    image: postgres:13-alpine  # versión vieja del usuario\n"
        "  custom-tool:\n"
        "    image: myorg/custom-tool:1.0\n"
    )
    rendered = s05.render_compose(make_config())
    merged = s05.merge_compose_preserving_user_edits(existing, rendered)
    data = _load_yaml(merged)
    assert data["services"]["custom-tool"]["image"] == "myorg/custom-tool:1.0"
    assert data["services"]["postgres"]["image"] == images.POSTGRES_IMAGE


def test_merge_compose_imposes_the_project_name_on_an_older_file() -> None:
    """El merge preserva las claves de nivel superior del usuario, pero `name`
    no puede quedarse fuera: un compose anterior a que el wizard lo fijara
    seguiría sin él para siempre, que es el estado en el que mover la carpeta
    pierde los volúmenes."""
    existing = "services:\n  postgres:\n    image: postgres:13-alpine\n"
    merged = s05.merge_compose_preserving_user_edits(existing, s05.render_compose(make_config()))
    assert _load_yaml(merged)["name"] == s05.PROJECT_NAME


def test_merge_compose_overwrites_a_hand_edited_project_name() -> None:
    """Cambiar `name` a mano reapunta el stack entero a otro juego de
    volúmenes: parece una instalación vacía y los datos siguen donde estaban."""
    existing = f"name: otro-nombre\nservices:\n  postgres:\n    image: {images.POSTGRES_IMAGE}\n"
    merged = s05.merge_compose_preserving_user_edits(existing, s05.render_compose(make_config()))
    assert _load_yaml(merged)["name"] == s05.PROJECT_NAME


def test_merge_compose_rejects_non_mapping_root_with_actionable_error() -> None:
    """Regresión: un compose cuya raíz no es un mapping hacía reventar el
    merge con un `TypeError` crudo en vez de un error accionable."""
    rendered = s05.render_compose(make_config())
    with pytest.raises(ConfigError, match="mapping en la raíz"):
        s05.merge_compose_preserving_user_edits("- just\n- a\n- list\n", rendered)


def test_merge_compose_rejects_invalid_yaml_with_actionable_error() -> None:
    rendered = s05.render_compose(make_config())
    with pytest.raises(ConfigError, match="no es YAML válido"):
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
    for name in s05.MANAGED_SERVICES:
        assert name in data["services"]


def test_merge_compose_handles_services_key_of_wrong_type() -> None:
    rendered = s05.render_compose(make_config())
    merged = s05.merge_compose_preserving_user_edits("services: not-a-mapping\n", rendered)
    data = _load_yaml(merged)
    assert data["services"]["postgres"]["image"] == images.POSTGRES_IMAGE


def test_merge_compose_preserves_comments() -> None:
    existing = "# comentario importante del usuario\nservices:\n  postgres:\n    image: old\n"
    rendered = s05.render_compose(make_config())
    merged = s05.merge_compose_preserving_user_edits(existing, rendered)
    assert "# comentario importante del usuario" in merged


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


# --- limpieza de Zone.Identifier ----------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="En NTFS 'archivo:Zone.Identifier' es un alternate data stream, no un "
    "archivo listable — el caso real solo se da en WSL/Linux, donde ':' es un "
    "carácter de nombre de archivo literal.",
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
    """Regresión: el `OSError` de un archivo bloqueado subía sin capturar y
    tumbaba el paso 5 entero —el que escribe compose, .env y el certificado—
    saliendo como `Error inesperado`. Es basura cosmética: se informa y se
    sigue.

    Se simula el recorrido en vez de crear el archivo porque en NTFS
    `a.txt:Zone.Identifier` es un alternate data stream y no aparece como
    archivo listable; lo que se prueba aquí es el manejo del error, que no
    depende del sistema de archivos.
    """
    mocker.patch(
        "axion_wizard.steps.s05_compose.os.walk",
        return_value=[(str(tmp_path), [], ["a.txt:Zone.Identifier"])],
    )
    mocker.patch.object(Path, "unlink", side_effect=OSError("en uso por otro proceso"))

    removed, failed = s05.clean_zone_identifier_files(tmp_path)

    assert removed == []
    assert failed == [tmp_path / "a.txt:Zone.Identifier"]


def test_clean_zone_identifier_does_not_descend_into_heavy_directories(
    tmp_path: Path, mocker
) -> None:
    """`.venv` y `.git` tienen decenas de miles de archivos y no pueden
    contener un Zone.Identifier que importe: recorrerlos convertía una
    limpieza instantánea en una pausa de segundos."""
    walked_dirnames: list[str] = ["fastapi", ".venv", ".git", "node_modules", "nginx"]
    mocker.patch(
        "axion_wizard.steps.s05_compose.os.walk",
        return_value=[(str(tmp_path), walked_dirnames, [])],
    )

    s05.clean_zone_identifier_files(tmp_path)

    # `os.walk` respeta la poda in-place de `dirnames`.
    assert walked_dirnames == ["fastapi", "nginx"]


# --- valores que sobreviven a un segundo `install` ------------------------------
#
# Regresión de una pérdida de datos silenciosa: `.env` se regeneraba entero en
# cada corrida, así que un segundo `install` —para cambiar el modelo, por
# ejemplo— borraba el token del webhook. fastapi volvía a aceptar cualquier
# llamada sin validarla, sin error, sin aviso y sin nada en los logs.


def test_render_env_preserves_the_webhook_token() -> None:
    text = s05.render_env(make_config(), preserved={"MM_WEBHOOK_TOKEN": "tok3n-real"})
    assert "MM_WEBHOOK_TOKEN=tok3n-real" in text


def test_render_env_preserves_the_system_prompt() -> None:
    text = s05.render_env(
        make_config(), preserved={"OLLAMA_SYSTEM_PROMPT": "Responde siempre en español."}
    )
    assert "OLLAMA_SYSTEM_PROMPT=Responde siempre en español." in text


def test_render_env_defaults_to_empty_when_nothing_to_preserve() -> None:
    text = s05.render_env(make_config())
    assert "MM_WEBHOOK_TOKEN=\n" in text
    assert "OLLAMA_SYSTEM_PROMPT=\n" in text


def test_preserved_env_values_reads_the_existing_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "POSTGRES_PASSWORD=abc\nMM_WEBHOOK_TOKEN=tok3n\nOLLAMA_SYSTEM_PROMPT=se breve\n",
        encoding="utf-8",
    )
    preserved = s05.preserved_env_values(tmp_path)
    assert preserved["MM_WEBHOOK_TOKEN"] == "tok3n"
    assert preserved["OLLAMA_SYSTEM_PROMPT"] == "se breve"


def test_preserved_env_values_empty_without_a_previous_env(tmp_path: Path) -> None:
    assert s05.preserved_env_values(tmp_path) == dict.fromkeys(s05.PRESERVED_ENV_KEYS, "")


def test_a_second_render_round_trip_keeps_the_token(tmp_path: Path) -> None:
    """El ciclo completo: `set-webhook-token` escribe, `install` regenera."""
    env_path = tmp_path / ".env"
    s05.write_secret_env_file(env_path, s05.render_env(make_config()))
    s05.update_env_value(env_path, "MM_WEBHOOK_TOKEN", "token-de-ejemplo-no-real-000")

    preserved = s05.preserved_env_values(tmp_path)
    s05.write_secret_env_file(env_path, s05.render_env(make_config(), preserved=preserved))

    assert "MM_WEBHOOK_TOKEN=token-de-ejemplo-no-real-000" in env_path.read_text(encoding="utf-8")


def test_write_secret_env_file_backs_up_the_previous_version(tmp_path: Path) -> None:
    """`.env` y `wg.env` llevan secretos y se regeneran enteros; eran los
    únicos archivos gestionados sin copia de seguridad."""
    env_path = tmp_path / ".env"
    env_path.write_text("POSTGRES_PASSWORD=viejo\n", encoding="utf-8")

    backup = s05.write_secret_env_file(env_path, "POSTGRES_PASSWORD=nuevo\n", backup=True)

    assert backup is not None
    assert backup.read_text(encoding="utf-8") == "POSTGRES_PASSWORD=viejo\n"
    assert env_path.read_text(encoding="utf-8") == "POSTGRES_PASSWORD=nuevo\n"


def test_write_secret_env_file_without_a_previous_version_has_no_backup(tmp_path: Path) -> None:
    assert s05.write_secret_env_file(tmp_path / ".env", "A=1\n", backup=True) is None


def test_existing_env_value_survives_a_file_it_cannot_decode(tmp_path: Path) -> None:
    """`OLLAMA_SYSTEM_PROMPT` invita a editar el `.env` a mano, y un editor de
    Windows guardando en ANSI produce bytes que no son UTF-8. Perder ese valor
    es malo; abortar la instalación entera por ello, peor."""
    (tmp_path / ".env").write_bytes(b"OLLAMA_SYSTEM_PROMPT=responde en espa\xf1ol\n")
    assert s05.existing_env_value(tmp_path, "OLLAMA_SYSTEM_PROMPT") is None
    assert s05.preserved_env_values(tmp_path) == dict.fromkeys(s05.PRESERVED_ENV_KEYS, "")


# --- el hash bcrypt debe sobrevivir a la interpolación de Compose -----------------
#
# Regresión de un fallo mudo y confirmado en vivo: `wg.env` contenía
# `PASSWORD_HASH=$2b$12$GktCd...` y al contenedor le llegaba
# `$2b$12.96FL219a`. Docker Compose SÍ interpola los valores de `env_file:`
# —al contrario de lo que afirmaba el comentario de la plantilla— y se comía
# `$GktCd...` como variable indefinida. El panel de wg-easy arrancaba sano y
# rechazaba cualquier contraseña, sin escribir nada en sus logs.


def test_wg_env_escapes_the_dollars_of_the_bcrypt_hash() -> None:
    config = make_config()
    raw_hash = config.wireguard_admin_password_hash.get_secret_value()
    text = s05.render_wg_env(config)

    assert raw_hash.startswith("$2b$")
    assert f"PASSWORD_HASH={raw_hash.replace('$', '$$')}" in text
    # y no queda ningún `$` suelto que Compose pueda interpretar
    hash_line = next(line for line in text.splitlines() if line.startswith("PASSWORD_HASH="))
    assert "$$2b$$12$$" in hash_line
    assert "$2b$12$" not in hash_line.replace("$$", "")


def test_wg_env_escaped_hash_round_trips_to_the_original() -> None:
    """Deshacer el escape debe devolver exactamente el hash bcrypt: es lo que
    hará Compose al pasárselo al contenedor."""
    config = make_config()
    raw_hash = config.wireguard_admin_password_hash.get_secret_value()
    hash_line = next(
        line
        for line in s05.render_wg_env(config).splitlines()
        if line.startswith("PASSWORD_HASH=")
    )
    escaped = hash_line.split("=", 1)[1]

    assert escaped.replace("$$", "$") == raw_hash
    assert len(escaped.replace("$$", "$")) == 60


def test_wg_host_is_not_mangled_by_the_escaping() -> None:
    text = s05.render_wg_env(make_config(host="192.168.1.50"))
    assert "WG_HOST=192.168.1.50" in text
