"""How the wizard looks on a terminal.

`console` owns the shared Rich `Console` and the `axion.*` theme; `ui` owns
the status glyphs and the table factory that every report is built from.

Both are used by the Rich CLI *and* by the Textual TUI — a `✓` means the
same thing in a `doctor` table as in the TUI's step list, which is what
makes the two interfaces feel like one application rather than two products
sharing a name.
"""
