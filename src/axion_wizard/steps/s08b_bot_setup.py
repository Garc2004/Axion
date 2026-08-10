"""Paso 8b — Bot y webhook de Mattermost, entre el cliente WireGuard y la
verificación final (§4.5.1, §4.8).

Ni el bot ni el webhook se pueden crear sin pasar por la interfaz web de
Mattermost: no hay API sin sesión, y la sesión exige una cuenta ya creada por
un admin humano — no hay forma de rodear eso desde un instalador. Este paso
no lo intenta: se detiene, explica los dos pasos exactos a seguir en el
panel, y guarda los tokens resultantes. Es la misma escritura que hacen
`set-bot-token`/`set-webhook-token` por separado después del despliegue,
movida a mitad del `install` para no depender de acordarse de correr dos
comandos más — y de llegar a hacerlo: mientras tanto la IA sigue en modo
síncrono, con el límite de tiempo del webhook.

Los dos son opcionales. Dejarlos en blanco no rompe nada: se aplican más
tarde con los mismos comandos, sin perder nada por haberlos omitido aquí.

Si se da un token de bot, también pregunta si la respuesta debe colgar del
mensaje que la disparó (en hilo, plegado hasta hacer clic) o publicarse como
mensaje normal del canal — `AI_REPLY_IN_THREAD` en `.env`, sin efecto en modo
síncrono.
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
    title = "Bot y webhook de Mattermost"
    #: Nada aquí puede "dejar de sostenerse" de un modo que importe entre
    #: reanudaciones: si el webhook deja de responder, `doctor` ya lo dice
    #: en su fila "Webhook alcanzable". Revalidar esto no protegería nada
    #: que esa comprobación no cubra ya.
    revalidate_on_resume = False

    def run(self) -> StepResult:
        config = self.context.require_config()

        if self.state.dry_run:
            console.print(
                "[axion.info][dry-run][/] preguntaría el token del bot y del "
                "webhook de Mattermost"
            )
            return StepResult(name=self.name, ok=True, message="omitido por --dry-run")

        console.print(
            "[axion.info]Bot y webhook de Mattermost[/] (opcional — se puede aplicar "
            "después con axion-wizard set-bot-token / set-webhook-token):"
        )
        console.print(
            f"  1. Entrar a https://{config.host} con la cuenta admin → "
            "Integraciones → Cuentas de bot → Crear, copiar su token."
        )
        console.print(
            "  2. Integraciones → Webhooks salientes → Crear, apuntando al canal "
            "donde debe responder la IA, copiar su token."
        )

        bot_token, webhook_token = self._collect_tokens()

        updates: dict[str, str] = {}
        if bot_token:
            updates[MM_BOT_TOKEN_KEY] = bot_token
            # Sin bot no hay modo asíncrono, y sin modo asíncrono esto no
            # tiene ningún efecto — de ahí que solo se pregunte aquí dentro.
            thread_preference = self._collect_thread_preference()
            if thread_preference is not None:
                updates[AI_REPLY_IN_THREAD_KEY] = "true" if thread_preference else "false"
        if webhook_token:
            updates[MM_WEBHOOK_TOKEN_KEY] = webhook_token

        if not updates:
            message = (
                "omitido: aplícalo cuando quieras con axion-wizard set-bot-token / "
                "set-webhook-token"
            )
            self.context.warn(message)
            return StepResult(name=self.name, ok=True, message=message)

        self._apply(updates)
        return StepResult(name=self.name, ok=True, message=f"aplicado: {', '.join(updates)}")

    def verify(self) -> StepResult:
        return StepResult(name=self.name, ok=True, message="sin verificación propia")

    # --- recolección de tokens ---------------------------------------------------

    def _collect_tokens(self) -> tuple[str | None, str | None]:
        if self.state.unattended:
            return self._tokens_from_config_file()
        if not interactive_input_available():
            return None, None
        return self._ask_token("bot"), self._ask_token("webhook saliente")

    def _tokens_from_config_file(self) -> tuple[str | None, str | None]:
        """En modo `--unattended` los tokens vienen del mismo `axion.toml` que
        el resto de la configuración.

        Se leen aparte de `AxionConfig` a propósito: son secretos que se
        aplican *después* del despliegue (§4.5), no parte de la
        configuración del stack, así que forzarlos dentro de ese modelo
        obligaría a cualquier otro lector de `AxionConfig` a saber de ellos
        sin necesidad.
        """
        raw = self._load_toml()
        if raw is None:
            return None, None
        return self._clean(raw.get("mm_bot_token")), self._clean(raw.get("mm_webhook_token"))

    def _ask_token(self, label: str) -> str | None:
        import questionary

        answer = questionary.text(f"Token del {label} (vacío para omitir):").ask()
        return self._clean(answer)

    def _collect_thread_preference(self) -> bool | None:
        """Si la respuesta va colgada del mensaje que la disparó (hilo,
        plegado hasta hacer clic) o se publica como mensaje normal del
        canal. `None` deja el valor por defecto del `.env` tal cual —
        `AI_REPLY_IN_THREAD` ya se preserva entre instalaciones como
        cualquier otro ajuste de este tipo, así que no hace falta forzar
        nada si no hay de dónde sacar una respuesta."""
        if self.state.unattended:
            return self._thread_preference_from_config_file()
        if not interactive_input_available():
            return None

        import questionary

        return questionary.confirm(
            "¿Colgar la respuesta del mensaje que la disparó, en vez de publicarla "
            "como mensaje normal del canal?",
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
            if cleaned in ("true", "1", "yes", "si", "sí"):
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
            secret_utils.validate_env_value(token, label="el token")
        except secret_utils.InvalidEnvValueError as exc:
            console.print(f"[axion.warn]Token con carácter no válido, se omite: {exc}[/]")
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
        console.print(f"[axion.ok]Guardado en {env_path}:[/] {', '.join(updates)}")

        console.print("[axion.dim]Recreando el contenedor fastapi para aplicarlo…[/]")
        s06_deploy.deploy(compose_path, services=[FASTAPI_SERVICE])
        s06_deploy.wait_for_healthy(compose_path, services=[FASTAPI_SERVICE])
        console.print("[axion.ok]Listo.[/]")

        if MM_BOT_TOKEN_KEY in updates:
            console.print(
                "[axion.dim]El bot tiene que estar añadido al equipo y a los canales "
                "donde deba responder, o Mattermost rechazará la publicación.[/]"
            )
