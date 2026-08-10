"""Restringe un archivo al usuario actual (§6.2).

`chmod 600` no tiene efecto real en Windows: ahí se aplica una ACL
restringida vía `icacls`. Usado tanto para la clave privada del certificado
como para `.env`/`wg.env`, que llevan secretos en claro.
"""

from __future__ import annotations

import os
import platform as _platform
from pathlib import Path

from axion_wizard.errors import ConfigError
from axion_wizard.utils.shell import run


def restrict_to_owner(path: Path, timeout: float = 15.0) -> None:
    if _platform.system() == "Windows":
        username = os.environ.get("USERNAME", "")
        if not username:
            raise ConfigError(
                what="No se pudo determinar el usuario de Windows actual",
                why=(
                    f"La variable de entorno USERNAME no está definida; sin ella no se "
                    f"puede restringir el ACL de {path.name}."
                ),
                steps=["Verificar que la variable de entorno USERNAME esté definida."],
            )
        result = run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{username}:F"],
            timeout=timeout,
        )
        if not result.ok:
            raise ConfigError(
                what=f"icacls falló restringiendo permisos de {path}",
                why=(
                    f"{path.name} quedaría con los permisos por defecto de Windows, "
                    "potencialmente legible por otros usuarios del sistema."
                ),
                steps=[
                    f'Ejecutar manualmente: icacls "{path}" /inheritance:r '
                    f'/grant:r "{username}:F"'
                ],
            )
    else:
        path.chmod(0o600)
