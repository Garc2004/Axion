"""Step 8b — Mattermost bot and webhook, between the WireGuard client and the
final verification (§4.5.1, §4.8).

Neither the bot nor the webhook can be created without going through
Mattermost's web interface: there is no API without a session, and a session
requires an account already created by a human admin — there is no way around
that from an installer. This step does not try: it stops, explains the two
exact steps to follow in the panel, and stores the resulting tokens. It is
the same write that `set-bot-token`/`set-webhook-token` perform separately
after deployment, moved into the middle of `install` so as not to depend on
remembering to run two more commands — and on getting round to it: in the
meantime the AI stays in synchronous mode, under the webhook's time limit.

Both are optional. Leaving them blank breaks nothing: they are applied later
with the same commands, with nothing lost by having skipped them here.

If a bot token is given, it also asks whether the reply should hang off the
message that triggered it (in a thread, collapsed until clicked) or be posted
as a normal channel message — `AI_REPLY_IN_THREAD` in `.env`, with no effect
in synchronous mode.
"""

from __future__ import annotations

from axion_wizard.render.console import console
from axion_wizard.steps.base import Step, StepResult
from axion_wizard.steps.prompts import interactive_input_available

MM_BOT_TOKEN_KEY = "MM_BOT_TOKEN"
MM_WEBHOOK_TOKEN_KEY = "MM_WEBHOOK_TOKEN"
AI_REPLY_IN_THREAD_KEY = "AI_REPLY_IN_THREAD"


class BotSetupStep(Step):
    name = "bot_setup"
    title = "Mattermost bot and webhook"
    #: Nothing here can "stop holding" in a way that matters between
    #: resumes: if the webhook stops answering, `doctor` already says so in
    #: its "Webhook reachable" row. Revalidating this would protect nothing
    #: that check does not already cover.
    revalidate_on_resume = False

    def run(self) -> StepResult:
        config = self.context.require_config()

        if self.state.dry_run:
            console.print(
                "[axion.info][dry-run][/] would ask for the Mattermost bot and "
                "webhook tokens"
            )
            return StepResult(name=self.name, ok=True, message="skipped by --dry-run")

        console.print(
            "[axion.info]Mattermost bot and webhook[/] (optional — can be applied "
            "later with axion-wizard set-bot-token / set-webhook-token):"
        )
        console.print(
            f"  1. Sign in at https://{config.host} with the admin account → "
            "Integrations → Bot Accounts → Create, and copy its token."
        )
        console.print(
            "  2. Integrations → Outgoing Webhooks → Create, pointing at the channel "
            "the AI should answer in, and copy its token."
        )

        bot_token, webhook_token = self._collect_tokens()

        updates: dict[str, str] = {}
        if bot_token:
            updates[MM_BOT_TOKEN_KEY] = bot_token
            # No bot means no asynchronous mode, and without asynchronous
            # mode this has no effect at all — hence only asking in here.
            thread_preference = self._collect_thread_preference()
            if thread_preference is not None:
                updates[AI_REPLY_IN_THREAD_KEY] = "true" if thread_preference else "false"
        if webhook_token:
            updates[MM_WEBHOOK_TOKEN_KEY] = webhook_token

        if not updates:
            message = (
                "skipped: apply it whenever you like with axion-wizard set-bot-token / "
                "set-webhook-token"
            )
            self.context.warn(message)
            return StepResult(name=self.name, ok=True, message=message)

        self._apply(updates)
        return StepResult(name=self.name, ok=True, message=f"applied: {', '.join(updates)}")

    def verify(self) -> StepResult:
        return StepResult(name=self.name, ok=True, message="no check of its own")

    # --- collecting the tokens ----------------------------------------------------

    def _collect_tokens(self) -> tuple[str | None, str | None]:
        if self.state.unattended:
            return self._tokens_from_config_file()
        if not interactive_input_available():
            return None, None
        return self._ask_token("bot"), self._ask_token("outgoing webhook")

    def _tokens_from_config_file(self) -> tuple[str | None, str | None]:
        """Under `--unattended` the tokens come from the same `axion.toml` as
        the rest of the configuration.

        They are read apart from `AxionConfig` on purpose: they are secrets
        applied *after* deployment (§4.5), not part of the stack's
        configuration, so forcing them into that model would oblige every
        other reader of `AxionConfig` to know about them for no reason.
        """
        raw = self._load_toml()
        if raw is None:
            return None, None
        return self._clean(raw.get("mm_bot_token")), self._clean(raw.get("mm_webhook_token"))

    def _ask_token(self, label: str) -> str | None:
        import questionary

        answer = questionary.text(f"{label.capitalize()} token (empty to skip):").ask()
        return self._clean(answer)

    def _collect_thread_preference(self) -> bool | None:
        """Whether the reply hangs off the message that triggered it (a
        thread, collapsed until clicked) or is posted as a normal channel
        message. `None` leaves the `.env` default as it is —
        `AI_REPLY_IN_THREAD` is already preserved across installs like any
        other setting of this kind, so there is no need to force anything when
        there is nowhere to get an answer from."""
        if self.state.unattended:
            return self._thread_preference_from_config_file()
        if not interactive_input_available():
            return None

        import questionary

        return questionary.confirm(
            "Hang the reply off the message that triggered it, rather than posting "
            "it as a normal channel message?",
            default=True,
        ).ask()

    def _thread_preference_from_config_file(self) -> bool | None:
        raw = self._load_toml()
        if raw is None:
            return None
        value = raw.get("ai_reply_in_thread")
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            cleaned = value.strip().lower()
            if cleaned in ("true", "1", "yes", "y"):
                return True
            if cleaned in ("false", "0", "no"):
                return False
        return None

    def _load_toml(self) -> dict | None:
        path = self.state.config_path
        if path is None or not path.exists():
            return None

        import tomllib

        try:
            return tomllib.loads(path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError):
            return None

    @staticmethod
    def _clean(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        token = value.strip()
        if not token:
            return None
        from axion_wizard.utils import secrets as secret_utils

        try:
            secret_utils.validate_env_value(token, label="the token")
        except secret_utils.InvalidEnvValueError as exc:
            console.print(f"[axion.warn]Token has an invalid character, skipping: {exc}[/]")
            return None
        return token

    def _apply(self, updates: dict[str, str]) -> None:
        from axion_wizard.domain.stack import FASTAPI_SERVICE
        from axion_wizard.steps import s06_deploy
        from axion_wizard.steps.s05_compose import update_env_value
        from axion_wizard.utils import secrets as secret_utils

        env_path = self.context.project_dir / ".env"
        compose_path = self.context.project_dir / "docker-compose.yml"

        for token in updates.values():
            secret_utils.register_secret(token)
        for key, value in updates.items():
            update_env_value(env_path, key, value)
        console.print(f"[axion.ok]Saved to {env_path}:[/] {', '.join(updates)}")

        console.print("[axion.dim]Recreating the fastapi container to apply it…[/]")
        s06_deploy.deploy(compose_path, services=[FASTAPI_SERVICE])
        s06_deploy.wait_for_healthy(compose_path, services=[FASTAPI_SERVICE])
        console.print("[axion.ok]Done.[/]")

        if MM_BOT_TOKEN_KEY in updates:
            console.print(
                "[axion.dim]The bot has to be added to the team and to every channel "
                "it should answer in, or Mattermost will reject the post.[/]"
            )
