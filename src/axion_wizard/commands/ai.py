"""`models`, `model` and the Mattermost token setters.

Everything that decides *what the AI is* and *how Mattermost reaches it*:
which model answers, with what standing instructions, and the two tokens
that switch the FastAPI bridge between its synchronous and asynchronous
modes.

Changing the model used to take three manual steps and knowing all three:
pull it, edit `OLLAMA_MODEL` in `.env` by hand, and recreate the fastapi
container with the right `docker compose` invocation (a `restart` will not
do: environment variables are fixed when the container is created).
Forgetting the third left the user staring at an AI still answering with
the old model, with no error anywhere. These commands do all three.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from axion_wizard.commands._common import announce_dry_run, compose_path_of
from axion_wizard.domain.stack import FASTAPI_SERVICE, WEBHOOK_CALLBACK_URL
from axion_wizard.errors import ConfigError
from axion_wizard.render import ui
from axion_wizard.render.console import console

if TYPE_CHECKING:
    from axion_wizard.cli import GlobalState



def _validated_token(token: str, *, label: str, source_hint: str) -> str:
    """Check a token can be written into a `.env` without breaking it."""
    from axion_wizard.utils import secrets as secret_utils

    token = token.strip()
    if not token:
        raise ConfigError(
            what=f"{label.capitalize()} cannot be empty",
            why="With no value there is nothing to write into .env.",
            steps=[source_hint],
        )
    try:
        secret_utils.validate_env_value(token, label=label)
    except secret_utils.InvalidEnvValueError as exc:
        raise ConfigError(
            what="The token contains an invalid character",
            why=str(exc),
            steps=["Check the whole token was copied, with no stray spaces or characters."],
        ) from exc
    return token



def run_set_webhook_token(state: GlobalState, token: str) -> None:
    """Store `MM_WEBHOOK_TOKEN` in `.env` and recreate fastapi so it picks it
    up (§4.5.1). Without this, enabling token validation meant hand-editing
    `.env` and remembering the exact `docker compose` incantation to apply it
    — precisely what this command spares you.
    """
    from axion_wizard.utils import secrets as secret_utils

    token = _validated_token(
        token,
        label="the token",
        source_hint=(
            "Create it in Mattermost: Integrations → Outgoing Webhooks → Create, "
            f"with {WEBHOOK_CALLBACK_URL} as the Callback URL — then copy its token."
        ),
    )
    env_path = state.project_dir / ".env"

    if state.dry_run:
        announce_dry_run(f"would write MM_WEBHOOK_TOKEN into {env_path} and recreate fastapi")
        return

    compose_path_of(state)
    secret_utils.register_secret(token)
    _apply_env_and_recreate_fastapi(state, {"MM_WEBHOOK_TOKEN": token})



def run_set_bot_token(state: GlobalState, token: str) -> None:
    """Store `MM_BOT_TOKEN` and switch the bridge into asynchronous mode.

    This is the switch between the two modes of `fastapi/main.py`: with a bot
    token, the bridge answers the webhook instantly and posts the reply
    through the API once the model finishes. Without it, Mattermost abandons
    the request after ~30s and a slow model's answer is lost whole, leaving no
    trace of why in any log.
    """
    from axion_wizard.utils import secrets as secret_utils

    token = _validated_token(
        token,
        label="the bot token",
        source_hint=(
            "Create a bot in Mattermost (Integrations → Bot Accounts) and copy its token."
        ),
    )
    env_path = state.project_dir / ".env"

    if state.dry_run:
        announce_dry_run(f"would write MM_BOT_TOKEN into {env_path} and recreate fastapi")
        return

    compose_path_of(state)
    secret_utils.register_secret(token)
    _apply_env_and_recreate_fastapi(state, {"MM_BOT_TOKEN": token})
    console.print(
        "[axion.ok]Asynchronous mode active:[/] the AI can now take as long as it "
        "needs; its answer will be posted to the channel as soon as it is ready."
    )
    console.print(
        "[axion.dim]The bot has to be added to the team and to every channel it "
        "should answer in, or Mattermost will reject the post.[/]"
    )



def run_models_list(state: GlobalState) -> None:
    """A three-tier catalogue, ordered by fit to the hardware (§5)."""
    from axion_wizard.detect.hardware import detect_hardware
    from axion_wizard.services import ollama

    hardware = detect_hardware()
    catalog = asyncio.run(
        ollama.build_catalog(ram_gb=hardware.ram_total_gb, has_gpu=hardware.has_gpu)
    )
    recommended = ollama.recommended_model(catalog, hardware.ram_total_gb, hardware.has_gpu)

    gpu_label = ", ".join(g.name or g.vendor for g in hardware.gpus) or "no dedicated GPU"
    console.print(
        f"[axion.info]Hardware detected:[/] {hardware.ram_total_gb:.1f} GB RAM, "
        f"{hardware.cpu_logical} cores, {gpu_label}"
    )

    table = ui.make_table("Ollama models")
    table.add_column("")
    table.add_column("Model", style="axion.label")
    table.add_column("Size")
    table.add_column("Min. RAM")
    table.add_column("Status", overflow="fold")

    for model in catalog:
        marker = ""
        if recommended is not None and model.name == recommended.name:
            marker = f"[axion.ok]{ui.GLYPH_OK}[/]"

        reason = ollama.suitability_reason(model, hardware.ram_total_gb, hardware.has_gpu)
        if model.installed:
            model_status = ui.ok("already installed")
        elif reason:
            # They are shown, not hidden: the user decides (§5).
            model_status = ui.warn(reason)
        else:
            model_status = "[axion.dim]compatible[/]"

        table.add_row(
            marker,
            model.name,
            f"{model.size_gb:.1f} GB" if model.size_bytes else "—",
            f"{model.min_ram_gb:g} GB" if ollama.has_known_requirements(model) else "?",
            model_status,
        )

    console.print(table)
    console.print(
        "[axion.dim]Download one with: axion-wizard models pull <name>. "
        "Ollama's library grows constantly: any valid name works, even one that does "
        "not appear in this list.[/]"
    )



def run_models_pull(state: GlobalState, name: str) -> None:
    """Download with a real progress bar, parsing the `/api/pull` stream."""
    from rich.progress import BarColumn, DownloadColumn, Progress, SpinnerColumn, TextColumn

    from axion_wizard.services import ollama

    if state.dry_run:
        announce_dry_run(f"would download model {name!r} via Ollama's API")
        return

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        console=console,
    ) as progress:
        task_id = progress.add_task(f"Downloading {name}", total=None)

        def on_progress(update: ollama.PullProgress) -> None:
            if update.total > 0:
                progress.update(
                    task_id,
                    total=update.total,
                    completed=update.completed,
                    description=f"{name} — {update.status}",
                )
            else:
                progress.update(task_id, description=f"{name} — {update.status}")

        asyncio.run(ollama.pull_model(name, on_progress))

    console.print(f"[axion.ok]Model downloaded:[/] {name}")
    console.print(
        f"[axion.dim]To make the AI use it: axion-wizard model set {name} "
        "(it writes .env and recreates fastapi for you).[/]"
    )



# --- editing the AI (§5) ---------------------------------------------------------
#
# Changing the model took three manual steps and knowing all three: pull it,
# hand-edit OLLAMA_MODEL in `.env`, and recreate the fastapi container with the
# right `docker compose` invocation (a `restart` will not do: environment
# variables are fixed when the container is created). Forgetting the third left
# the user staring at an AI still answering with the old model, with no error.
#
# `axion-wizard model` does all three.


def _apply_env_and_recreate_fastapi(state: GlobalState, updates: dict[str, str]) -> None:
    """Write keys into `.env` and recreate fastapi so it picks them up.

    The same path as `run_set_webhook_token`: `update_env_value` preserves the
    rest of the file, and `s06_deploy` brings the container up and waits for
    it to be healthy again rather than taking it as good the moment it
    launches.
    """
    from axion_wizard.steps import s06_deploy
    from axion_wizard.steps.s05_compose import update_env_value

    compose_path = compose_path_of(state)
    env_path = state.project_dir / ".env"

    for key, value in updates.items():
        update_env_value(env_path, key, value)
    console.print(f"[axion.ok]Saved to {env_path}:[/] {', '.join(updates)}")

    console.print("[axion.dim]Recreating the fastapi container to apply it…[/]")
    s06_deploy.deploy(compose_path, services=[FASTAPI_SERVICE])
    s06_deploy.wait_for_healthy(compose_path, services=[FASTAPI_SERVICE])
    console.print("[axion.ok]Done. The AI is now using the new configuration.[/]")



def _current_ai_settings(state: GlobalState) -> dict[str, str]:
    from axion_wizard.steps.s05_compose import existing_env_value

    return {
        "OLLAMA_MODEL": existing_env_value(state.project_dir, "OLLAMA_MODEL") or "(unset)",
        "OLLAMA_SYSTEM_PROMPT": existing_env_value(state.project_dir, "OLLAMA_SYSTEM_PROMPT")
        or "(no instructions: the model answers from its own training)",
    }



def run_model_show(state: GlobalState) -> None:
    """Which model and which instructions the AI is using right now."""
    compose_path_of(state)  # fails with a clear message if there is no stack
    current = _current_ai_settings(state)

    table = ui.make_table("Current AI configuration")
    table.add_column("Setting", style="axion.label")
    table.add_column("Value", overflow="fold")
    table.add_row("Model", f"[axion.info]{current['OLLAMA_MODEL']}[/]")
    table.add_row("Instructions", current["OLLAMA_SYSTEM_PROMPT"])
    console.print(table)
    console.print(
        "[axion.dim]Change the model: axion-wizard model set <name> "
        "(or `model choose` to pick from a list).\n"
        'Change the instructions: axion-wizard model prompt "<text>"[/]'
    )



def run_model_set(state: GlobalState, name: str, skip_pull: bool = False) -> None:
    """Change the AI's model end to end: pull, `.env`, and recreate."""
    from axion_wizard.services import ollama

    name = name.strip()
    if not name:
        raise ConfigError(
            what="No model was given",
            why="The model's name in Ollama is required (e.g. `qwen2.5:1.5b`).",
            steps=[
                "See what fits this hardware: axion-wizard models",
                "Pick one from a list: axion-wizard model choose",
            ],
        )

    if state.dry_run:
        announce_dry_run(
            f"would download {name!r} if needed, write OLLAMA_MODEL into .env "
            "and recreate fastapi"
        )
        return

    compose_path = compose_path_of(state)
    already_installed = name in ollama.installed_model_names(
        asyncio.run(ollama.list_installed_models())
    )

    if already_installed:
        console.print(f"[axion.ok]Model {name} is already downloaded.[/]")
    elif skip_pull:
        console.print(
            f"[axion.warn]{name} is not downloaded and downloading was declined:[/] "
            "the AI will fail until it is."
        )
    else:
        # Without this the failure arrives later and elsewhere: fastapi starts
        # with a model Ollama does not have, and every message returns an error
        # that mentions the missing download nowhere.
        run_models_pull(state, name=name)

    _apply_env_and_recreate_fastapi(state, {"OLLAMA_MODEL": name})
    console.print(f"[axion.dim]Check the whole thing: axion-wizard doctor ({compose_path}).[/]")



def run_model_choose(state: GlobalState) -> None:
    """Pick the model from a list ordered by fit to the hardware (§5).

    This is the path for anyone who does not know the names by heart: the same
    three-tier catalogue and the same order as step 3 of the installer.
    """
    import questionary

    from axion_wizard.detect.hardware import detect_hardware
    from axion_wizard.services import ollama
    from axion_wizard.steps.prompts import require_interactive_input
    from axion_wizard.steps.s03_config import build_model_choices

    compose_path_of(state)
    require_interactive_input("Choosing the model interactively")

    current = _current_ai_settings(state)["OLLAMA_MODEL"]
    console.print(f"[axion.info]Current model:[/] {current}")

    hardware = detect_hardware()
    catalog = asyncio.run(
        ollama.build_catalog(ram_gb=hardware.ram_total_gb, has_gpu=hardware.has_gpu)
    )
    recommended = ollama.recommended_model(catalog, hardware.ram_total_gb, hardware.has_gpu)
    choices, default = build_model_choices(catalog, recommended, hardware)

    answer = questionary.select(
        "Which model should the AI use?", choices=choices, default=default
    ).ask()
    if answer is None:
        console.print("[axion.warn]No changes.[/]")
        return
    if answer == ollama.OTHER_MODEL_SENTINEL:
        answer = (
            questionary.text("Model name in Ollama:").ask() or ""
        ).strip()
        if not answer:
            console.print("[axion.warn]No changes.[/]")
            return

    run_model_set(state, name=str(answer))



def run_model_prompt(state: GlobalState, prompt: str) -> None:
    """Edit the AI's standing instructions (tone, language, role).

    They are passed to Ollama as `system` on every request, so they apply to
    every conversation without the user having to repeat them each time.
    """
    from axion_wizard.utils import secrets as secret_utils

    prompt = prompt.strip()
    try:
        secret_utils.validate_env_value(prompt, label="the instructions")
    except secret_utils.InvalidEnvValueError as exc:
        raise ConfigError(
            what="The instructions contain a character that would break the .env",
            why=str(exc),
            steps=["Rewrite them without `$`, backtick or `!`."],
        ) from exc

    if state.dry_run:
        action = "would clear the AI's instructions" if not prompt else "would write the new ones"
        announce_dry_run(f"{action} in .env and recreate fastapi")
        return

    compose_path_of(state)
    _apply_env_and_recreate_fastapi(state, {"OLLAMA_SYSTEM_PROMPT": prompt})
    if prompt:
        console.print(f"[axion.dim]Active instructions: {prompt}[/]")
    else:
        console.print(
            "[axion.dim]Instructions cleared: the AI goes back to answering from its "
            "own training.[/]"
        )
