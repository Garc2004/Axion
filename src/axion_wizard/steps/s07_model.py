"""Step 7 — AI model (§4.7, §5).

Downloads the model chosen in step 3 with a real progress bar, parsing the
line-delimited JSON stream of `/api/pull`, and recreates the FastAPI
container so it picks up the `OLLAMA_MODEL` already written into `.env`.

Ollama starts cold: the container can be `running` while its API is not yet
accepting requests. Hence waiting for it to answer before pulling the model,
rather than failing with a connection error that would look like a network
problem.
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
    title = "AI model"

    def run(self) -> StepResult:
        config = self.context.require_config()
        model_name = config.ollama_model

        if self.state.dry_run:
            console.print(
                f"[axion.info][dry-run][/] would download model {model_name!r} "
                "and recreate the fastapi container"
            )
            return StepResult(name=self.name, ok=True, message="skipped by --dry-run")

        if asyncio.run(self._model_already_installed(model_name)):
            console.print(f"[axion.ok]Model {model_name} is already downloaded.[/]")
        else:
            asyncio.run(self._wait_for_ollama())
            self._pull_with_progress(model_name)

        self._recreate_fastapi()
        return StepResult(name=self.name, ok=True, message=f"model {model_name} available")

    def verify(self) -> StepResult:
        if self.state.dry_run:
            return StepResult(name=self.name, ok=True, message="skipped by --dry-run")

        model_name = self.context.require_config().ollama_model
        if asyncio.run(self._model_already_installed(model_name)):
            return StepResult(name=self.name, ok=True, message=model_name)
        return StepResult(
            name=self.name, ok=False, message=f"{model_name} is not among the installed models"
        )

    # --- internals -------------------------------------------------------------------

    @staticmethod
    async def _model_already_installed(model_name: str) -> bool:
        installed = await ollama.list_installed_models()
        return model_name in ollama.installed_model_names(installed)

    @staticmethod
    async def _wait_for_ollama(timeout: float = DEFAULT_READY_TIMEOUT) -> None:
        """Wait out Ollama's cold start with exponential backoff.

        `list_installed_models` does not raise when the server is unreachable:
        it returns an empty list. This tells "not answering" apart from
        "answering with no models" by watching whether the request completes
        at all.
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
                what="The Ollama server did not answer in time",
                why=(
                    f"{timeout:g}s elapsed waiting on "
                    f"{ollama.OLLAMA_LOCAL_BASE_URL}/api/tags after bringing the stack up."
                ),
                steps=[
                    "Check the container: docker compose ps ollama",
                    "Read its logs: axion-wizard logs ollama",
                    "Retry: axion-wizard install (it resumes at this step)",
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
            task_id = progress.add_task(f"Downloading {model_name}", total=None)

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

        console.print(f"[axion.ok]Model downloaded:[/] {model_name}")

    def _recreate_fastapi(self) -> None:
        """§5: recreate the FastAPI container so it picks up `OLLAMA_MODEL`.

        A `restart` would not do: environment variables are fixed when the
        container is created, so the old value would survive the restart.

        This goes through `s06_deploy` rather than assembling the
        `docker compose` invocation by hand — which is what it used to do —
        for two reasons: not duplicating the knowledge of how Compose is
        invoked (`run_set_webhook_token` already uses this path), and because
        doing so also waits for fastapi to be *healthy* again. Without that
        wait, step 9 could verify the stack while the freshly recreated
        container was still starting and report a failure that did not exist.

        A problem here is not fatal: the model is already downloaded and the
        rest of the stack is up, so it warns and carries on.
        """
        from axion_wizard.errors import AxionError
        from axion_wizard.steps import s06_deploy

        compose_path = self.context.project_dir / "docker-compose.yml"
        try:
            s06_deploy.deploy(compose_path, services=[FASTAPI_SERVICE])
            s06_deploy.wait_for_healthy(compose_path, services=[FASTAPI_SERVICE])
        except AxionError as exc:
            self.context.warn(
                "Could not recreate the fastapi container; it may still be using the "
                f"previous model: {exc}"
            )
            return
        console.print("[axion.ok]fastapi container recreated with the configured model.[/]")
