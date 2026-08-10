"""Paso 7 — Modelo de IA (§4.7, §5).

Descarga el modelo elegido en el paso 3 con barra de progreso real,
parseando el stream JSON-por-línea de `/api/pull`, y recrea el contenedor de
FastAPI para que tome el `OLLAMA_MODEL` ya escrito en `.env`.

Ollama arranca en frío: el contenedor puede estar `running` y su API todavía
no aceptar peticiones. Por eso se espera a que responda antes de tirar del
modelo, en vez de fallar con un error de conexión que parecería un problema
de red.
"""

from __future__ import annotations

import asyncio

from rich.progress import BarColumn, DownloadColumn, Progress, SpinnerColumn, TextColumn
from tenacity import AsyncRetrying, RetryError, retry_if_result, stop_after_delay, wait_exponential

from axion_wizard.domain.stack import FASTAPI_SERVICE
from axion_wizard.errors import OllamaError
from axion_wizard.render.console import console
from axion_wizard.services import ollama
from axion_wizard.steps.base import Step, StepResult

DEFAULT_READY_TIMEOUT = 120.0


class ModelStep(Step):
    name = "model"
    title = "Modelo de IA"

    def run(self) -> StepResult:
        config = self.context.require_config()
        model_name = config.ollama_model

        if self.state.dry_run:
            console.print(
                f"[axion.info][dry-run][/] descargaría el modelo {model_name!r} "
                "y recrearía el contenedor fastapi"
            )
            return StepResult(name=self.name, ok=True, message="omitido por --dry-run")

        if asyncio.run(self._model_already_installed(model_name)):
            console.print(f"[axion.ok]El modelo {model_name} ya está descargado.[/]")
        else:
            asyncio.run(self._wait_for_ollama())
            self._pull_with_progress(model_name)

        self._recreate_fastapi()
        return StepResult(name=self.name, ok=True, message=f"modelo {model_name} disponible")

    def verify(self) -> StepResult:
        if self.state.dry_run:
            return StepResult(name=self.name, ok=True, message="omitido por --dry-run")

        model_name = self.context.require_config().ollama_model
        if asyncio.run(self._model_already_installed(model_name)):
            return StepResult(name=self.name, ok=True, message=model_name)
        return StepResult(
            name=self.name, ok=False, message=f"{model_name} no aparece entre los instalados"
        )

    # --- interno ---------------------------------------------------------------------

    @staticmethod
    async def _model_already_installed(model_name: str) -> bool:
        installed = await ollama.list_installed_models()
        return model_name in ollama.installed_model_names(installed)

    @staticmethod
    async def _wait_for_ollama(timeout: float = DEFAULT_READY_TIMEOUT) -> None:
        """Espera al arranque en frío de Ollama con backoff exponencial.

        `list_installed_models` no lanza si el servidor no responde: devuelve
        lista vacía. Aquí se distingue "no responde" de "responde y no tiene
        modelos" mirando si la petición llega a completarse.
        """
        import httpx

        async def _responds() -> bool:
            async with httpx.AsyncClient(timeout=5.0) as client:
                try:
                    response = await client.get(f"{ollama.OLLAMA_LOCAL_BASE_URL}/api/tags")
                except httpx.HTTPError:
                    return False
                return response.status_code < 500

        retryer = AsyncRetrying(
            stop=stop_after_delay(timeout),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_result(lambda ready: ready is False),
            reraise=False,
        )
        try:
            await retryer(_responds)
        except RetryError as exc:
            raise OllamaError(
                what="El servidor de Ollama no respondió a tiempo",
                why=(
                    f"Se agotaron {timeout:g}s esperando a "
                    f"{ollama.OLLAMA_LOCAL_BASE_URL}/api/tags tras levantar el stack."
                ),
                steps=[
                    "Comprobar el contenedor: docker compose ps ollama",
                    "Revisar sus logs: axion-wizard logs ollama",
                    "Reintentar: axion-wizard install (se reanuda en este paso)",
                ],
            ) from exc

    def _pull_with_progress(self, model_name: str) -> None:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            console=console,
        ) as progress:
            task_id = progress.add_task(f"Descargando {model_name}", total=None)

            def on_progress(update: ollama.PullProgress) -> None:
                if update.total > 0:
                    progress.update(
                        task_id,
                        total=update.total,
                        completed=update.completed,
                        description=f"{model_name} — {update.status}",
                    )
                else:
                    progress.update(task_id, description=f"{model_name} — {update.status}")

            asyncio.run(ollama.pull_model(model_name, on_progress))

        console.print(f"[axion.ok]Modelo descargado:[/] {model_name}")

    def _recreate_fastapi(self) -> None:
        """§5: recrear el contenedor de FastAPI para que tome `OLLAMA_MODEL`.

        Un `restart` no bastaría: las variables de entorno se fijan al crear
        el contenedor, así que el valor viejo sobreviviría al reinicio.

        Va por `s06_deploy` en vez de armar el `docker compose` a mano —que
        es lo que hacía— por dos motivos: no duplicar el conocimiento de cómo
        se invoca Compose (`run_set_webhook_token` ya usa este camino), y
        porque así además se espera a que fastapi vuelva a estar *sano*. Sin
        esa espera, el paso 9 podía verificar el stack mientras el contenedor
        recién recreado seguía arrancando y reportar un fallo que no existía.

        Un problema aquí no es fatal: el modelo ya está descargado y el resto
        del stack en pie, así que se avisa y se sigue.
        """
        from axion_wizard.errors import AxionError
        from axion_wizard.steps import s06_deploy

        compose_path = self.context.project_dir / "docker-compose.yml"
        try:
            s06_deploy.deploy(compose_path, services=[FASTAPI_SERVICE])
            s06_deploy.wait_for_healthy(compose_path, services=[FASTAPI_SERVICE])
        except AxionError as exc:
            self.context.warn(
                "No se pudo recrear el contenedor fastapi; puede seguir usando el "
                f"modelo anterior: {exc}"
            )
            return
        console.print("[axion.ok]Contenedor fastapi recreado con el modelo configurado.[/]")
