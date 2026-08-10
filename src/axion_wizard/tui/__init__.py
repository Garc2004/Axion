"""Full-screen (Textual) interface for the install flow.

**An alternative, not a replacement.** §1.3 of the spec rules Textual out for
the linear install flow, and that decision stands: `axion-wizard install`
uses questionary and Rich as it always has. This is what runs only under
`install --tui`.

The design sidesteps the underlying conflict between the two — questionary
and Textual fight over the terminal — by collecting *all* configuration in a
form before starting, and then running the ten steps in unattended mode: that
way no step tries to open a prompt while Textual owns the screen. The
bot/webhook step has no form of its own here — under unattended mode it is
simply skipped, and applied afterwards with
`set-bot-token`/`set-webhook-token` or `doctor`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from axion_wizard.cli import GlobalState


def run_tui_install(state: GlobalState) -> bool:
    """Start the TUI and return whether the install finished cleanly.

    The import lives inside so that Textual — which drags in its own
    dependency tree — is not loaded for an `axion-wizard --version`.
    """
    from axion_wizard.tui.app import AxionInstallerApp

    app = AxionInstallerApp(state)
    app.run()
    return app.succeeded
