"""Guards for the steps that ask something.

Without a real terminal, `questionary` does not fail in any readable way: on
Windows it raises `NoConsoleScreenBufferError("No Windows console found. Are
you running cmd.exe?")`, and on POSIX it either waits forever or returns
`None` halfway through the flow. Neither tells the user what to do, and the
first used to surface through the generic handler as `Unexpected error: …`,
which is exactly what §8 forbids.
"""

from __future__ import annotations

import sys

from axion_wizard.errors import ConfigError


def interactive_input_available() -> bool:
    """`True` if there is a terminal on the other end to ask.

    **Both stdin and stdout** are required. It might look as though stdin
    alone would do, since the point is to read an answer, but questionary
    sits on prompt_toolkit, which *draws* the prompt: on Windows it needs a
    real console screen buffer and, if output is redirected, it blows up with
    `NoConsoleScreenBufferError` even while stdin is still a TTY. Checking
    only stdin let `axion-wizard install > log.txt` through, and the flow died
    halfway through step 2.
    """
    for stream in (sys.stdin, sys.stdout):
        try:
            if stream is None or not stream.isatty():
                return False
        except (AttributeError, OSError, ValueError):
            return False
    return True


def require_interactive_input(what: str) -> None:
    """Raise an actionable `ConfigError` if nothing can be asked."""
    if interactive_input_available():
        return
    raise ConfigError(
        what=f"{what} needs an interactive terminal",
        why=(
            "Input or output is not a terminal (a pipe, a redirection, or CI), so the "
            "prompt cannot be drawn and the answer cannot be read."
        ),
        steps=[
            "Run it directly in a terminal, with no pipes or redirections.",
            "Or without prompts: axion-wizard install --unattended --config axion.toml",
        ],
    )
