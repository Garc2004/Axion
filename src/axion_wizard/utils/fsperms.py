"""Restrict a file to the current user (§6.2).

`chmod 600` has no real effect on Windows: there, a restricted ACL is applied
via `icacls` instead. Used both for the certificate's private key and for
`.env`/`wg.env`, which hold secrets in the clear.
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
                what="Could not determine the current Windows user",
                why=(
                    f"The USERNAME environment variable is not set; without it the ACL "
                    f"on {path.name} cannot be restricted."
                ),
                steps=["Check that the USERNAME environment variable is set."],
            )
        result = run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{username}:F"],
            timeout=timeout,
        )
        if not result.ok:
            raise ConfigError(
                what=f"icacls failed while restricting permissions on {path}",
                why=(
                    f"{path.name} would be left with Windows' default permissions, "
                    "potentially readable by other users on the system."
                ),
                steps=[
                    f'Run it by hand: icacls "{path}" /inheritance:r '
                    f'/grant:r "{username}:F"'
                ],
            )
    else:
        path.chmod(0o600)
