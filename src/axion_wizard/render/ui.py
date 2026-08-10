"""Visual vocabulary shared by the Rich output (CLI) and the Textual TUI.

Before this module, every table in the wizard — `network-check`, `models`,
`doctor`, the detected environment, the TUI's progress screen — defined its
own colours and status symbols. The result was five surfaces that did not
feel like the same application: some said "OK"/"FAIL" in plain text, others
used glyphs (`✓`/`✕`) only in the TUI, and panel borders mixed loose colour
names (`"cyan"`, `"red"`) instead of the semantic tokens already defined in
`console.py`.

This is the single definition of "what an OK looks like" and "what a wizard
report table looks like", so that any new screen reuses it rather than
reinventing it.
"""

from __future__ import annotations

from rich import box
from rich.table import Table

# --- Status glyphs ------------------------------------------------------------------
#
# The same six states the TUI's progress screen uses (`tui/app.py`). A ✓
# meaning the same thing in a `doctor` table as in the step list of
# `install --tui` is what makes the two interfaces feel like one application
# rather than two products sharing a name.
GLYPH_OK = "✓"
GLYPH_FAIL = "✕"
GLYPH_WARN = "▲"
GLYPH_PENDING = "○"
GLYPH_RUNNING = "◐"
GLYPH_SKIPPED = "−"

#: Glyph + Rich style per state, for callers that need the two separately
#: (the TUI, which turns this into a Textual colour rather than Rich markup).
STATUS_STYLE: dict[str, tuple[str, str]] = {
    "ok": (GLYPH_OK, "axion.ok"),
    "fail": (GLYPH_FAIL, "axion.error"),
    "warn": (GLYPH_WARN, "axion.warn"),
    "pending": (GLYPH_PENDING, "axion.dim"),
    "running": (GLYPH_RUNNING, "axion.info"),
    "skipped": (GLYPH_SKIPPED, "axion.dim"),
}


def ok(label: str = "OK") -> str:
    """Positive status marker, ready for a Rich table cell."""
    return f"[axion.ok]{GLYPH_OK} {label}[/]"


def fail(label: str = "FAIL") -> str:
    return f"[axion.error]{GLYPH_FAIL} {label}[/]"


def warn(label: str) -> str:
    return f"[axion.warn]{GLYPH_WARN} {label}[/]"


def status(passed: bool, ok_label: str = "OK", fail_label: str = "FAIL") -> str:
    """Shortcut for the commonest case across the wizard's tables: a boolean
    OK/FAIL column."""
    return ok(ok_label) if passed else fail(fail_label)


# --- Tables and panels ----------------------------------------------------------------


def make_table(title: str) -> Table:
    """A table with the look common to every report in the wizard.

    Centralising it here is what stops `network-check`, `models`, `doctor` and
    the detected environment (§4.1) from looking like four tables from four
    different libraries: same box, same header style, same title and border
    colour.
    """
    return Table(
        title=title,
        title_style="axion.heading",
        # A theme style name, not a compound one ("bold axion.accent"): Rich
        # only resolves a style name against the theme when it stands *alone*
        # — a compound string tries to parse each word as a literal colour and
        # blows up with `MissingStyle` as soon as it fails to recognise
        # "axion.accent" as a valid colour.
        header_style="axion.heading",
        box=box.ROUNDED,
        border_style="axion.border",
        pad_edge=False,
    )
