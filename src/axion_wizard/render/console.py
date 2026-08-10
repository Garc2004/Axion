"""The wizard's shared Rich instance and colour theme."""

from __future__ import annotations

import ctypes
import io
import sys

from rich.console import Console, RenderableType
from rich.theme import Theme

#: Windows UTF-8 codepage, for `SetConsoleOutputCP`.
_WINDOWS_UTF8_CODEPAGE = 65001


def configure_stdio_encoding() -> None:
    """Force UTF-8 on stdout/stderr before anything is printed.

    The Windows console defaults to a regional codepage (cp1252 in western
    Europe) that cannot represent much of what this wizard prints: the `✓` on
    the recommended model (§5) and the Unicode blocks of the WireGuard QR
    code (§4.8). Without this, `models` and `wireguard add-client` do not
    merely look wrong — they die with `UnicodeEncodeError` mid-output.

    Two distinct, complementary things happen: `reconfigure` avoids the
    encoding error, and `SetConsoleOutputCP` additionally makes the console
    draw them correctly. `errors="replace"` leaves the worst case as an ugly
    character rather than an exception.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue  # e.g. stdout captured by pytest
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

    if sys.platform == "win32":
        try:
            ctypes.windll.kernel32.SetConsoleOutputCP(_WINDOWS_UTF8_CODEPAGE)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass


configure_stdio_encoding()

AXION_THEME = Theme(
    {
        # --- Status semantics: the same meaning on every screen -------------------
        "axion.ok": "bold green",
        "axion.warn": "bold yellow",
        "axion.error": "bold red",
        "axion.info": "bold cyan",
        # --- Structure: borders, headers and titles, not states -------------------
        #
        # Kept apart from "info" on purpose: "info" is for text the user should
        # read as data (a URL, say); "accent" is for the visual scaffolding
        # (table borders, headers) that should register without competing with
        # the content for attention.
        "axion.accent": "cyan",
        "axion.border": "cyan",
        "axion.heading": "bold cyan",
        "axion.label": "bold",
        # --- Secondary text and secrets ---------------------------------------------
        "axion.dim": "grey62",
        "axion.secret": "grey42",
        # --- Section banner: a filled block, for one title per screen --------------
        "axion.title": "bold white on blue",
    }
)

console = Console(theme=AXION_THEME)
error_console = Console(theme=AXION_THEME, stderr=True)


def set_quiet(quiet: bool) -> None:
    console.quiet = quiet


def set_no_color(no_color: bool) -> None:
    if no_color:
        console.no_color = True
        error_console.no_color = True


def render_to_ansi(renderable: RenderableType, width: int = 100) -> str:
    """Render a renderable with the AXION theme and return ANSI text.

    This exists for the TUI. Textual draws its `RichLog` with a `Console` of
    its own, which does not know the `axion.*` theme: writing a `Panel` that
    uses `axion.label` into it blows up with `MissingStyle` — it does not look
    wrong, it crashes.

    Resolving the theme here, where it is defined, leaves the TUI with nothing
    to do but replay the already-resolved colours. That is what lets the two
    interfaces share one renderer (`render_closing_summary`) instead of
    rewriting its content by hand in each.
    """
    buffer = Console(
        theme=AXION_THEME,
        width=width,
        file=io.StringIO(),
        record=True,
        force_terminal=True,
    )
    buffer.print(renderable)
    return buffer.export_text(styles=True)
