# -*- mode: python ; coding: utf-8 -*-
"""Spec de PyInstaller compartida por Windows y Linux (§7).

No hay cross-compilation: el .exe se construye en Windows, el binario ELF en
Linux — cada plataforma corre este mismo spec con SU PROPIO PyInstaller
(dos jobs de CI: `windows-latest` y `ubuntu-22.04`, ver §7.2).
"""

import os
import sys
from pathlib import Path

SPEC_DIR = Path(SPECPATH)  # noqa: F821 - inyectado por PyInstaller al ejecutar el spec
PROJECT_ROOT = SPEC_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
TEMPLATES_DIR = SRC_DIR / "axion_wizard" / "templates"

# Las plantillas deben declararse en `datas`: importlib.resources solo las
# resuelve si el bundle las incluye explícitamente. Nunca se leen por ruta
# relativa a __file__ (utils/resources.py) — esa ruta apunta a un directorio
# temporal distinto en cada ejecución dentro de un bundle --onefile (§7.2).
datas = [
    (str(template_file), str(template_file.parent.relative_to(SRC_DIR)))
    for template_file in TEMPLATES_DIR.rglob("*")
    if template_file.is_file()
]

# ruamel.yaml y pydantic cargan submódulos dinámicamente; sin declararlos
# aquí el binario falla en runtime con ModuleNotFoundError (§7.2).
hiddenimports = [
    "ruamel.yaml",
    "pydantic",
    "pydantic_core",
]

# Textual (`install --tui`) resuelve widgets y drivers por nombre en tiempo de
# ejecución, así que el análisis estático de PyInstaller no los ve. Se
# recogen módulos y datos (su CSS interno va como recurso del paquete).
try:
    from PyInstaller.utils.hooks import collect_data_files, collect_submodules

    hiddenimports += collect_submodules("textual")
    datas += collect_data_files("textual")
except ImportError:  # pragma: no cover - solo si cambia la API de PyInstaller
    pass

a = Analysis(  # noqa: F821 - inyectado por PyInstaller
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

pyz = PYZ(a.pure, a.zipped_data)  # noqa: F821 - inyectado por PyInstaller

exe_name = "axion-wizard.exe" if sys.platform == "win32" else "axion-wizard-linux-x86_64"

# Modo --onefile: a.binaries/a.zipfiles/a.datas van directo a EXE(), sin
# COLLECT() (ese es el modo --onedir).
exe = EXE(  # noqa: F821 - inyectado por PyInstaller
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
    # Elevación: por defecto NO se incrusta el manifiesto requireAdministrator.
    #
    # Ese manifiesto se aplica al *lanzar* el .exe, antes de que corra una
    # sola línea de Python, y en un CLI eso hace más daño que bien:
    #   - desde una consola sin elevar, el .exe ni siquiera arranca
    #     ("Acceso denegado") — se cae hasta `--version`;
    #   - cuando UAC sí se acepta, Windows abre una consola *nueva* y la
    #     cierra al terminar, así que la salida del comando no se ve;
    #   - `--unattended` en CI deja de funcionar por completo.
    #
    # El camino por defecto es `privileges.ensure_elevated`, que eleva solo
    # para los subcomandos que lo necesitan y explica por qué antes.
    # Quien quiera igualmente un binario siempre-elevado puede construirlo
    # con AXION_UAC_ADMIN=1.
    uac_admin=(sys.platform == "win32" and os.environ.get("AXION_UAC_ADMIN") == "1"),
)
