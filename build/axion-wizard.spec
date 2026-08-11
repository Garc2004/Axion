# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec shared by Windows and Linux (§7).

There is no cross-compilation: the .exe is built on Windows, the ELF binary on
Linux — each platform runs this same spec with ITS OWN PyInstaller (two CI
jobs: `windows-latest` and `ubuntu-22.04`, see §7.2).
"""

import os
import sys
from pathlib import Path

SPEC_DIR = Path(SPECPATH)  # noqa: F821 - injected by PyInstaller when running the spec
PROJECT_ROOT = SPEC_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
TEMPLATES_DIR = SRC_DIR / "axion_wizard" / "templates"

# The templates have to be declared in `datas`: importlib.resources only
# resolves them if the bundle includes them explicitly. They are never read by a
# path relative to __file__ (utils/resources.py) — inside a --onefile bundle
# that path points at a different temporary directory on every run (§7.2).
datas = [
    (str(template_file), str(template_file.parent.relative_to(SRC_DIR)))
    for template_file in TEMPLATES_DIR.rglob("*")
    if template_file.is_file()
]

# ruamel.yaml and pydantic load submodules dynamically; without declaring them
# here the binary fails at runtime with ModuleNotFoundError (§7.2).
hiddenimports = [
    "ruamel.yaml",
    "pydantic",
    "pydantic_core",
]

# Textual (`install --tui`) resolves widgets and drivers by name at runtime, so
# PyInstaller's static analysis cannot see them. Both modules and data are
# collected (its internal CSS ships as a package resource).
try:
    from PyInstaller.utils.hooks import collect_data_files, collect_submodules

    hiddenimports += collect_submodules("textual")
    datas += collect_data_files("textual")
except ImportError:  # pragma: no cover - only if PyInstaller's API changes
    pass

a = Analysis(  # noqa: F821 - injected by PyInstaller
    [str(SRC_DIR / "axion_wizard" / "__main__.py")],
    pathex=[str(SRC_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)  # noqa: F821 - injected by PyInstaller

exe_name = "axion-wizard.exe" if sys.platform == "win32" else "axion-wizard-linux-x86_64"

# --onefile mode: a.binaries/a.zipfiles/a.datas go straight into EXE(), with no
# COLLECT() (that is --onedir mode).
exe = EXE(  # noqa: F821 - injected by PyInstaller
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name=exe_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    # Elevation: by default the requireAdministrator manifest is NOT embedded.
    #
    # That manifest is applied when the .exe is *launched*, before a single line
    # of Python runs, and in a CLI that does more harm than good:
    #   - from a non-elevated console the .exe will not even start ("Access
    #     denied") — down to and including `--version`;
    #   - when UAC is accepted, Windows opens a *new* console and closes it on
    #     exit, so the command's output is never seen;
    #   - `--unattended` in CI stops working entirely.
    #
    # The default path is `privileges.ensure_elevated`, which elevates only for
    # the subcommands that need it and explains why beforehand.
    # Anyone who wants an always-elevated binary anyway can build one with
    # AXION_UAC_ADMIN=1.
    uac_admin=(sys.platform == "win32" and os.environ.get("AXION_UAC_ADMIN") == "1"),
)
