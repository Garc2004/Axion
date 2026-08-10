"""Implementations of the CLI subcommands.

Each `run_*` here is what `cli.py` invokes for one subcommand: glue between
the CLI's options and the services (`services/`) and steps (`steps/`), with
no business logic of its own.

This used to be a single 800-line `steps/runner.py`, which put it in the one
package whose name it did not fit: of its eighteen entry points only
`run_install` has anything to do with the install steps. `steps/` now holds
the install flow and nothing else.

The split is by what the user is trying to do, not by which service is
involved — `model set` pulls from Ollama, writes `.env` and recreates a
container, and all three belong to the same intent.

Re-exported here so `cli.py` keeps importing from one place; the imports
stay inside each command body so that `--version` does not pay for loading
httpx, cryptography and questionary.
"""

from axion_wizard.commands.ai import (
    run_model_choose,
    run_model_prompt,
    run_model_set,
    run_model_show,
    run_models_list,
    run_models_pull,
    run_set_bot_token,
    run_set_webhook_token,
)
from axion_wizard.commands.diagnose import run_doctor, run_gen_cert, run_network_check
from axion_wizard.commands.install import run_install, run_reset
from axion_wizard.commands.lifecycle import (
    run_compose_down,
    run_compose_logs,
    run_compose_up,
    run_uninstall,
)
from axion_wizard.commands.vpn import run_wireguard_add_client

__all__ = [
    "run_compose_down",
    "run_compose_logs",
    "run_compose_up",
    "run_doctor",
    "run_gen_cert",
    "run_install",
    "run_model_choose",
    "run_model_prompt",
    "run_model_set",
    "run_model_show",
    "run_models_list",
    "run_models_pull",
    "run_network_check",
    "run_reset",
    "run_set_bot_token",
    "run_set_webhook_token",
    "run_uninstall",
    "run_wireguard_add_client",
]
