from pathlib import Path

import pytest

from axion_wizard.errors import ConfigError
from axion_wizard.utils import fsperms
from axion_wizard.utils.shell import CommandResult


def test_restrict_to_owner_posix_uses_chmod(mocker, tmp_path: Path) -> None:
    target = tmp_path / "secret.env"
    target.write_text("SECRET=1")
    mocker.patch("axion_wizard.utils.fsperms._platform.system", return_value="Linux")
    chmod_mock = mocker.patch.object(Path, "chmod")
    fsperms.restrict_to_owner(target)
    chmod_mock.assert_called_once_with(0o600)


def test_restrict_to_owner_windows_ok(mocker, monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "secret.env"
    target.write_text("SECRET=1")
    monkeypatch.setenv("USERNAME", "Perseus")
    mocker.patch("axion_wizard.utils.fsperms._platform.system", return_value="Windows")
    run_mock = mocker.patch(
        "axion_wizard.utils.fsperms.run",
        return_value=CommandResult(args=[], returncode=0, stdout="", stderr=""),
    )
    fsperms.restrict_to_owner(target)
    run_mock.assert_called_once()
    called_args = run_mock.call_args[0][0]
    assert called_args[0] == "icacls"
    assert "Perseus:F" in called_args


def test_restrict_to_owner_windows_missing_username(mocker, monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "secret.env"
    target.write_text("SECRET=1")
    monkeypatch.delenv("USERNAME", raising=False)
    mocker.patch("axion_wizard.utils.fsperms._platform.system", return_value="Windows")
    with pytest.raises(ConfigError, match="usuario de Windows"):
        fsperms.restrict_to_owner(target)


def test_restrict_to_owner_windows_icacls_fails(mocker, monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "secret.env"
    target.write_text("SECRET=1")
    monkeypatch.setenv("USERNAME", "Perseus")
    mocker.patch("axion_wizard.utils.fsperms._platform.system", return_value="Windows")
    mocker.patch(
        "axion_wizard.utils.fsperms.run",
        return_value=CommandResult(args=[], returncode=1, stdout="", stderr="access denied"),
    )
    with pytest.raises(ConfigError, match="icacls"):
        fsperms.restrict_to_owner(target)
