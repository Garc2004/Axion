"""Paso 6 — Despliegue (§4.6).

- `docker compose up -d --build`, con la salida en streaming mapeada a
  barras de progreso de Rich (una por servicio).
- Espera de healthchecks con `tenacity`: reintentos con backoff exponencial,
  timeout global configurable (300s por defecto).
- `ollama` no tiene healthcheck propio: se confirma que su servidor HTTP
  interno responde ejecutando `ollama list` dentro del contenedor — hace
  internamente el mismo `GET /api/tags` que exige la spec, sin depender de
  que la imagen traiga `curl`/`wget` instalados.
- En caso de fallo, `compose.build_deployment_failure_error` arma el error
  con las últimas 30 líneas del log del contenedor que falló.
- Tras el despliegue, se verifica que wg-easy no haya resuelto a v15 (§6.4).
"""

from __future__ import annotations

import re
from pathlib import Path

from rich.progress import BarColumn, Progress, SpinnerColumn, TaskID, TextColumn
from tenacity import RetryError, Retrying, retry_if_result, stop_after_delay, wait_exponential

from axion_wizard import images
from axion_wizard.console import console
from axion_wizard.errors import DeploymentError
from axion_wizard.services import compose
from axion_wizard.steps.base import Step, StepResult

DEFAULT_UP_TIMEOUT = 900.0
DEFAULT_HEALTHCHECK_TIMEOUT = 300.0
OLLAMA_SERVICE = "ollama"
WIREGUARD_SERVICE = "wireguard"
NGINX_SERVICE = "nginx"

#: Servicios que nginx tiene escritos por nombre en su configuración y que,
#: por tanto, resuelve por DNS una sola vez: al cargarla. Ver `refresh_nginx`.
NGINX_UPSTREAM_SERVICES = frozenset({"mattermost"})

_DONE_STATUSES = frozenset({"Started", "Healthy", "Running", "Built"})

_PROGRESS_LINE_RE = re.compile(
    r"^\s*(?:Container\s+)?(?P<name>\S+)\s+(?P<status>Pulling|Pulled|Building|Built|"
    r"Creating|Created|Starting|Started|Waiting|Healthy|Running|Removing|Removed|"
    r"Stopping|Stopped|Error)\b"
)


def parse_progress_line(line: str) -> tuple[str, str] | None:
    """Extrae `(nombre_contenedor_o_servicio, estado)` de una línea típica de
    `docker compose up --build` (p.ej. `Container axion-postgres-1  Started`).
    Devuelve `None` si la línea no coincide con el formato esperado."""
    match = _PROGRESS_LINE_RE.match(line)
    if not match:
        return None
    return match.group("name"), match.group("status")


def _match_task(tasks: dict[str, TaskID], container_or_service_name: str) -> TaskID | None:
    """`docker compose` reporta nombres de contenedor tipo `<proyecto>-<servicio>-<n>`;
    buscamos qué servicio gestionado aparece como substring."""
    for service_name, task_id in tasks.items():
        if service_name in container_or_service_name:
            return task_id
    return None


def deploy(compose_path: Path, services: list[str], timeout: float = DEFAULT_UP_TIMEOUT) -> None:
    """`docker compose up -d --build` con una barra de progreso por servicio.
    Lanza `DeploymentError` con el log del contenedor que falló si el
    comando termina con código distinto de cero."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}", justify="left"),
        BarColumn(),
        TextColumn("{task.fields[status]}"),
        console=console,
    ) as progress:
        tasks: dict[str, TaskID] = {
            name: progress.add_task(name, total=1, status="esperando...") for name in services
        }

        def on_line(line: str) -> None:
            parsed = parse_progress_line(line)
            if parsed is None:
                return
            name, status = parsed
            task_id = _match_task(tasks, name)
            if task_id is not None:
                completed = 1 if status in _DONE_STATUSES else 0
                progress.update(task_id, status=status, completed=completed)

        result = compose.up(compose_path, on_line=on_line, timeout=timeout)

    if not result.ok:
        failed_service = _first_not_running_service(compose_path, services) or services[0]
        raise compose.build_deployment_failure_error(compose_path, failed_service)


def refresh_nginx(compose_path: Path, services: list[str]) -> bool:
    """Reinicia nginx tras recrear alguno de sus upstreams, para que relea el
    certificado y reencuentre a Mattermost en el acto.

    El motivo de fondo —que nginx se quedaba con la primera IP que resolvió y
    dejaba todo el stack en 502— lo ataja ya la propia configuración de nginx,
    que re-resuelve en cada petición (ver `nginx-mattermost.conf.j2`). Esto
    sigue haciendo falta por dos cosas que aquella no cubre:

    1. El certificado. `nginx/certs` entra por bind mount, así que el archivo
       nuevo ya está dentro del contenedor, pero nginx sirve el que cargó en
       memoria al arrancar. Un `install` que regenere el certificado lo dejaba
       sin efecto hasta el siguiente reinicio.
    2. El hueco de re-resolución. El `resolver` cachea unos segundos; sin este
       reinicio, un `install` puede terminar anunciando que todo está listo
       mientras las siguientes peticiones aún dan 502.

    Devuelve si hizo falta reiniciar.
    """
    if not NGINX_UPSTREAM_SERVICES.intersection(services):
        # No se tocó ningún upstream: la IP que nginx tiene resuelta sigue
        # siendo válida. Si lo que se recreó fue el propio nginx, arrancó
        # después de sus upstreams y ya resolvió bien.
        return False

    status = compose.get_service_status(compose_path, NGINX_SERVICE)
    if status is None or not status.is_running:
        # Sin nginx en marcha no hay nada que refrescar; si toca levantarlo,
        # lo hará con la configuración y las IPs actuales.
        return False

    result = compose.restart(compose_path, NGINX_SERVICE)
    if not result.ok:
        raise compose.build_deployment_failure_error(compose_path, NGINX_SERVICE)
    return True


def _first_not_running_service(compose_path: Path, services: list[str]) -> str | None:
    statuses = _service_statuses(compose_path)
    for name in services:
        status = statuses.get(name)
        if status is None or not status.is_running:
            return name
    return None


def check_ollama_ready(
    compose_path: Path, service: str = OLLAMA_SERVICE, timeout: float = 10.0
) -> bool:
    """`ollama list` consulta internamente el mismo endpoint que
    `GET /api/tags`; usarlo evita depender de que la imagen traiga
    herramientas HTTP instaladas."""
    result = compose.exec_in_service(compose_path, service, ["ollama", "list"], timeout=timeout)
    return result.ok


def _service_is_ready(
    compose_path: Path, service: str, statuses: dict[str, compose.ContainerStatus]
) -> bool:
    status = statuses.get(service)
    if status is None or not status.is_running:
        return False
    if service == OLLAMA_SERVICE:
        return check_ollama_ready(compose_path, service)
    return status.is_healthy_or_no_healthcheck


def _service_statuses(compose_path: Path) -> dict[str, compose.ContainerStatus]:
    return {status.service: status for status in compose.ps(compose_path)}


def _all_services_ready(compose_path: Path, services: list[str]) -> bool:
    """Un solo `docker compose ps` por ronda, no uno por servicio.

    `get_service_status` lanza internamente un `ps` completo y se queda con
    una fila, así que preguntarlo servicio a servicio multiplicaba por seis
    el número de invocaciones a Docker en cada reintento — y esto corre en
    bucle con backoff hasta cinco minutos. Con un `ps` por ronda la
    información es exactamente la misma, además de coherente entre
    servicios: antes cada consulta veía un instante distinto.
    """
    statuses = _service_statuses(compose_path)
    return all(_service_is_ready(compose_path, name, statuses) for name in services)


def wait_for_healthy(
    compose_path: Path,
    services: list[str],
    timeout: float = DEFAULT_HEALTHCHECK_TIMEOUT,
    wait_min: float = 1.0,
    wait_max: float = 15.0,
) -> None:
    """Reintenta con backoff exponencial hasta que todos los `services`
    reporten listos, o hasta agotar `timeout` segundos en total.
    `wait_min`/`wait_max` acotan el backoff — configurables para poder
    probar la lógica de reintento sin esperas reales de segundos."""
    retryer = Retrying(
        stop=stop_after_delay(timeout),
        wait=wait_exponential(multiplier=wait_min, min=wait_min, max=wait_max),
        retry=retry_if_result(lambda ready: ready is False),
        reraise=False,
    )
    try:
        retryer(_all_services_ready, compose_path, services)
    except RetryError as exc:
        failed_service = _first_not_ready_service(compose_path, services) or services[0]
        raise compose.build_deployment_failure_error(compose_path, failed_service) from exc


def _first_not_ready_service(compose_path: Path, services: list[str]) -> str | None:
    statuses = _service_statuses(compose_path)
    for name in services:
        if not _service_is_ready(compose_path, name, statuses):
            return name
    return None


#: Un hash bcrypt son 60 caracteres: `$2b$` + coste + `$` + 22 de sal + 31 de
#: digest. Cualquier otra longitud significa que llegó mutilado.
BCRYPT_HASH_LENGTH = 60


def verify_password_hash_reached_the_container(
    compose_path: Path, service: str = WIREGUARD_SERVICE, timeout: float = 20.0
) -> None:
    """Comprueba que wg-easy recibió el hash bcrypt entero.

    Se lee del contenedor en marcha en vez de fiarse de lo que se escribió en
    `wg.env`, por el mismo motivo que el SAN del certificado se relee del
    archivo: entre lo que uno escribe y lo que el otro lado ve hay una capa
    que puede transformarlo. Aquí esa capa es la interpolación de variables de
    Docker Compose, que convierte `$2b$12$GktCd...` en `$2b$12.96FL219a` si el
    `$` no va escapado.

    El fallo que atrapa no da ninguna señal por sí solo: el contenedor arranca
    sano, el panel responde, y lo único que pasa es que ninguna contraseña
    entra nunca.
    """
    result = compose.exec_in_service(
        compose_path, service, ["printenv", "PASSWORD_HASH"], timeout=timeout
    )
    if not result.ok:
        # No se pudo preguntar (contenedor sin `printenv`, exec denegado). No
        # es motivo para dar por fallido el despliegue.
        return

    received = result.stdout.strip()
    if received.startswith("$2") and len(received) == BCRYPT_HASH_LENGTH:
        return

    raise DeploymentError(
        what="El panel de WireGuard recibió una contraseña corrupta",
        why=(
            f"`PASSWORD_HASH` llegó al contenedor con {len(received)} caracteres en vez de "
            f"{BCRYPT_HASH_LENGTH}: Docker Compose interpretó los `$` del hash bcrypt como "
            "variables y se los comió. Con el hash roto, el panel arranca y responde con "
            "normalidad pero rechaza cualquier contraseña, sin escribir nada en sus logs."
        ),
        steps=[
            "Regenerar los archivos del proyecto: axion-wizard install",
            "Comprobar que en wg.env el hash lleva los `$` escapados como `$$`.",
            f"Recrear el contenedor: axion-wizard up {service}",
        ],
    )


def verify_wg_easy_tag(compose_path: Path, service: str = WIREGUARD_SERVICE) -> None:
    """Verifica la tag *efectiva* del contenedor wg-easy ya desplegado (§6.4),
    no solo la que quedó escrita en el `docker-compose.yml` generado."""
    status = compose.get_service_status(compose_path, service)
    if status is None or not status.image:
        return
    _repo, tag = images.split_image_tag(status.image)
    # Sin tag explícita Docker resuelve a `latest`, que hoy es v15 — el caso
    # exacto que §6.4 existe para atajar, así que se trata como tal.
    effective_tag = tag if tag is not None else "latest"
    try:
        images.assert_wg_easy_tag_is_safe(effective_tag)
    except images.UnsafeWgEasyTagError as exc:
        raise DeploymentError(
            what=f"wg-easy está corriendo una tag insegura: {status.image}",
            why=str(exc),
            steps=[
                "Detener el stack: axion-wizard down",
                f"Verificar que docker-compose.yml fija la imagen en {images.WIREGUARD_IMAGE}",
                "Volver a desplegar: axion-wizard up",
            ],
        ) from exc


class DeployStep(Step):
    """Levanta el stack y espera a que esté realmente operativo (§4.6).

    "Arrancado" no es lo mismo que "listo": `docker compose up -d` vuelve en
    cuanto los contenedores existen, mucho antes de que PostgreSQL acepte
    conexiones o Mattermost responda. De ahí que tras el despliegue se espere
    a los healthchecks con backoff antes de dar el paso por bueno.
    """

    name = "deploy"
    title = "Despliegue"

    def run(self) -> StepResult:
        from axion_wizard.steps.s05_compose import managed_services_for_project

        compose_path = self.context.project_dir / "docker-compose.yml"
        # Del compose recién escrito, no de una constante: así la lista incluye
        # n8n exactamente cuando el despliegue lo lleva.
        services = list(managed_services_for_project(self.context.project_dir))

        if self.state.dry_run:
            console.print(
                "[axion.info][dry-run][/] ejecutaría `docker compose up -d --build` "
                f"para {', '.join(services)}"
            )
            self._ensure_host_ip_forwarding()
            return StepResult(name=self.name, ok=True, message="omitido por --dry-run")

        self._ensure_host_ip_forwarding()
        deploy(compose_path, services=services)
        wait_for_healthy(compose_path, services=services)
        # Antes de dar el despliegue por bueno: si Mattermost se recreó, nginx
        # sigue apuntando a la IP anterior y todo el stack devuelve 502 pese a
        # estar los seis contenedores healthy.
        if refresh_nginx(compose_path, services):
            console.print("[axion.ok]nginx reiniciado[/] para reencontrar a Mattermost.")
        # La tag que importa es la del contenedor en marcha, no la escrita en
        # el compose: quien lo edite a mano puede acabar en wg-easy v15, que
        # ignora WG_HOST/PASSWORD_HASH sin un solo error en los logs (§6.4).
        verify_wg_easy_tag(compose_path)
        # Y la contraseña que importa es la que el contenedor recibió, no la
        # que se escribió en wg.env: Compose interpola los `$` del hash.
        verify_password_hash_reached_the_container(compose_path)

        console.print("[axion.ok]Stack levantado y saludable.[/]")
        return StepResult(
            name=self.name, ok=True, message=f"{len(services)} servicios operativos"
        )

    def _ensure_host_ip_forwarding(self) -> None:
        """Activa el reenvío IP del host antes de levantar nada (§6.1).

        Solo aplica a Linux nativo con `network_mode: host`. Va *antes* del
        despliegue porque es configuración del kernel del host, no del
        contenedor: con el reenvío apagado, wg-easy arranca sin quejarse,
        el túnel se establece y el handshake funciona — pero ningún paquete
        llega a su destino, sin un solo error en ningún log.

        Un fallo aquí no aborta: la VPN quedaría sin encaminar, pero
        Mattermost, la IA y el acceso por LAN funcionan igual. Se avisa con
        los pasos manuales y se sigue.
        """
        from axion_wizard.services import hostnet

        environment = self.context.require_environment()
        if not hostnet.is_applicable(
            environment.os_info.name, environment.wireguard_variant
        ):
            return

        result = hostnet.ensure_ip_forwarding(dry_run=self.state.dry_run)

        if self.state.dry_run:
            console.print(f"[axion.info][dry-run][/] {result.detail}")
            return
        if not result.needs_attention:
            console.print(f"[axion.ok]Reenvío IP del host:[/] {result.detail}")
            return

        message = (
            "El reenvío IP del host no quedó activo "
            f"({result.detail}). El túnel de WireGuard se establecerá y el panel "
            "mostrará el cliente como conectado, pero ningún paquete llegará a su "
            "destino — y no aparecerá error en ningún log."
        )
        self.context.warn(message)
        console.print(f"[axion.warn]{message}[/]")
        for step in hostnet.describe_manual_fix():
            console.print(f"[axion.dim]  - {step}[/]")

    def verify(self) -> StepResult:
        from axion_wizard.services import compose as compose_service
        from axion_wizard.steps.s05_compose import managed_services_for_project

        if self.state.dry_run:
            return StepResult(name=self.name, ok=True, message="omitido por --dry-run")

        compose_path = self.context.project_dir / "docker-compose.yml"
        statuses = {s.service: s for s in compose_service.ps(compose_path)}
        unhealthy = [
            name
            for name in managed_services_for_project(self.context.project_dir)
            if name not in statuses
            or not statuses[name].is_running
            or not statuses[name].is_healthy_or_no_healthcheck
        ]
        if unhealthy:
            return StepResult(
                name=self.name, ok=False, message=f"no operativos: {', '.join(unhealthy)}"
            )
        return StepResult(name=self.name, ok=True, message="todos los servicios operativos")
