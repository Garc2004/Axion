import subprocess
import sys

import pytest

from axion_wizard import privileges as priv

# --- is_elevated ---------------------------------------------------------------


def test_is_elevated_true_on_windows_when_api_says_so(mocker) -> None:
    mocker.patch("axion_wizard.privileges.is_windows", return_value=True)
    fake_shell32 = mocker.Mock()
    fake_shell32.IsUserAnAdmin.return_value = 1
    mocker.patch("axion_wizard.privileges.ctypes.windll", create=True).shell32 = fake_shell32
    assert priv.is_elevated() is True


def test_is_elevated_false_on_windows_when_api_says_so(mocker) -> None:
    mocker.patch("axion_wizard.privileges.is_windows", return_value=True)
    fake_shell32 = mocker.Mock()
    fake_shell32.IsUserAnAdmin.return_value = 0
    mocker.patch("axion_wizard.privileges.ctypes.windll", create=True).shell32 = fake_shell32
    assert priv.is_elevated() is False


def test_is_elevated_conservative_when_windows_api_unavailable(mocker) -> None:
    """Si no se puede determinar, asumir 'sin elevar' — como mucho se ofrece
    elevar de más, nunca se da por elevado un proceso que no lo está."""
    mocker.patch("axion_wizard.privileges.is_windows", return_value=True)
    fake_windll = mocker.patch("axion_wizard.privileges.ctypes.windll", create=True)
    type(fake_windll).shell32 = mocker.PropertyMock(side_effect=OSError("no shell32"))
    assert priv.is_elevated() is False


def test_is_elevated_true_as_root_on_posix(mocker) -> None:
    mocker.patch("axion_wizard.privileges.is_windows", return_value=False)
    mocker.patch("axion_wizard.privileges.os.geteuid", create=True, return_value=0)
    assert priv.is_elevated() is True


def test_is_elevated_false_as_normal_user_on_posix(mocker) -> None:
    mocker.patch("axion_wizard.privileges.is_windows", return_value=False)
    mocker.patch("axion_wizard.privileges.os.geteuid", create=True, return_value=1000)
    assert priv.is_elevated() is False


def test_is_elevated_never_raises_on_this_machine() -> None:
    assert isinstance(priv.is_elevated(), bool)


# --- current_invocation ---------------------------------------------------------


def test_current_invocation_in_dev_mode_reinvokes_the_module(mocker) -> None:
    mocker.patch("axion_wizard.privileges.running_as_frozen_binary", return_value=False)
    mocker.patch.object(sys, "argv", ["axion-wizard", "install", "--verbose"])
    executable, args = priv.current_invocation()
    assert executable == sys.executable
    assert args == ["-m", "axion_wizard", "install", "--verbose"]


def test_current_invocation_when_frozen_reinvokes_the_binary(mocker) -> None:
    """En un bundle, sys.executable ya es el propio axion-wizard.exe: volver a
    pasarle `-m axion_wizard` lo rompería."""
    mocker.patch("axion_wizard.privileges.running_as_frozen_binary", return_value=True)
    mocker.patch.object(sys, "argv", ["axion-wizard.exe", "doctor"])
    executable, args = priv.current_invocation()
    assert executable == sys.executable
    assert args == ["doctor"]


# --- explain_elevation_reason ----------------------------------------------------


def test_explain_elevation_reason_lists_concrete_reasons() -> None:
    text = priv.explain_elevation_reason()
    assert "sysctl" in text
    assert "firewall" in text
    for reason in priv.ELEVATION_REASONS:
        assert reason in text


# --- _quote_windows_arg -----------------------------------------------------------


@pytest.mark.parametrize(
    ("arg", "expected"),
    [
        ("install", "install"),
        ("--verbose", "--verbose"),
        ("", '""'),
        (r"C:\Program Files\axion", r'"C:\Program Files\axion"'),
        ('has"quote', r'"has\"quote"'),
        # Una ruta acabada en barra: sin duplicarla, la barra escaparía la
        # comilla de cierre y se comería el resto de la línea de comandos.
        ("C:\\mis proyectos\\axion\\", '"C:\\mis proyectos\\axion\\\\"'),
        # Sin espacios ni comillas no hace falta comillar nada.
        ("C:\\axion\\", "C:\\axion\\"),
        # Barras que no preceden a una comilla se dejan tal cual.
        ('C:\\con espacio\\a\\b"c', '"C:\\con espacio\\a\\b\\"c"'),
    ],
)
def test_quote_windows_arg(arg: str, expected: str) -> None:
    assert priv._quote_windows_arg(arg) == expected


# --- relaunch_elevated_windows ------------------------------------------------------


def _fake_elevation(mocker, *, handle=1234, exit_code=0):
    start = mocker.patch("axion_wizard.privileges._start_elevated_process", return_value=handle)
    wait = mocker.patch("axion_wizard.privileges._wait_for_process", return_value=exit_code)
    return start, wait


def test_relaunch_elevated_windows_waits_and_propagates_exit_code(mocker) -> None:
    """El padre no puede terminar en cuanto dispara UAC: el trabajo real
    ocurre en el proceso elevado, y su resultado es el único que importa."""
    mocker.patch(
        "axion_wizard.privileges.current_invocation",
        return_value=("axion-wizard.exe", ["install"]),
    )
    start, wait = _fake_elevation(mocker, handle=99, exit_code=3)

    assert priv.relaunch_elevated_windows(working_dir=r"C:\axion") == 3

    executable, params, working_dir = start.call_args[0]
    assert executable == "axion-wizard.exe"
    assert params == "install"
    assert working_dir == r"C:\axion"
    wait.assert_called_once_with(99)


def test_relaunch_elevated_windows_prepends_leading_args(mocker) -> None:
    """`--project-dir` es una opción del grupo raíz: detrás del subcomando,
    Click la rechazaría con 'No such option'."""
    mocker.patch("axion_wizard.privileges.running_as_frozen_binary", return_value=True)
    mocker.patch.object(sys, "argv", ["axion-wizard.exe", "install", "--unattended"])
    start, _ = _fake_elevation(mocker)

    priv.relaunch_elevated_windows(leading_args=["--project-dir", r"C:\mis cosas\axion"])

    params = start.call_args[0][1]
    assert params == '--project-dir "C:\\mis cosas\\axion" install --unattended'


def test_relaunch_elevated_windows_defaults_working_dir_to_cwd(mocker) -> None:
    mocker.patch(
        "axion_wizard.privileges.current_invocation", return_value=("axion-wizard.exe", [])
    )
    mocker.patch("axion_wizard.privileges.os.getcwd", return_value=r"C:\actual")
    start, _ = _fake_elevation(mocker)

    priv.relaunch_elevated_windows()

    assert start.call_args[0][2] == r"C:\actual"


def test_relaunch_elevated_windows_without_handle_does_not_wait(mocker) -> None:
    mocker.patch(
        "axion_wizard.privileges.current_invocation", return_value=("axion-wizard.exe", [])
    )
    _, wait = _fake_elevation(mocker, handle=0)

    assert priv.relaunch_elevated_windows() == 0
    wait.assert_not_called()


def test_relaunch_elevated_windows_propagates_elevation_error(mocker) -> None:
    mocker.patch(
        "axion_wizard.privileges.current_invocation", return_value=("axion-wizard.exe", [])
    )
    mocker.patch(
        "axion_wizard.privileges._start_elevated_process",
        side_effect=priv.ElevationError("el usuario canceló el diálogo de UAC"),
    )
    with pytest.raises(priv.ElevationError, match="canceló"):
        priv.relaunch_elevated_windows()


def test_relaunch_elevated_windows_wraps_ctypes_failure(mocker) -> None:
    mocker.patch(
        "axion_wizard.privileges.current_invocation", return_value=("axion-wizard.exe", [])
    )
    mocker.patch(
        "axion_wizard.privileges._start_elevated_process", side_effect=OSError("sin shell32")
    )
    with pytest.raises(priv.ElevationError, match="ShellExecuteExW"):
        priv.relaunch_elevated_windows()


# --- relaunch_elevated_posix ---------------------------------------------------------


def test_relaunch_elevated_posix_builds_sudo_command(mocker) -> None:
    mocker.patch(
        "axion_wizard.privileges.current_invocation",
        return_value=("/usr/bin/python3", ["-m", "axion_wizard", "install"]),
    )
    run_mock = mocker.patch(
        "axion_wizard.privileges.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0),
    )
    assert priv.relaunch_elevated_posix() == 0
    command = run_mock.call_args[0][0]
    assert command[:2] == ["sudo", "-E"]
    assert "install" in command
    assert run_mock.call_args.kwargs["shell"] is False


def test_relaunch_elevated_posix_prepends_leading_args_and_sets_cwd(mocker) -> None:
    mocker.patch("axion_wizard.privileges.running_as_frozen_binary", return_value=False)
    mocker.patch.object(sys, "argv", ["axion-wizard", "install"])
    run_mock = mocker.patch(
        "axion_wizard.privileges.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0),
    )
    priv.relaunch_elevated_posix(
        leading_args=["--project-dir", "/srv/axion"], working_dir="/srv/axion"
    )
    command = run_mock.call_args[0][0]
    # `--project-dir` va detrás de `-m axion_wizard`: delante lo interpretaría
    # Python como argumento suyo, no del wizard.
    assert command == [
        "sudo",
        "-E",
        sys.executable,
        "-m",
        "axion_wizard",
        "--project-dir",
        "/srv/axion",
        "install",
    ]
    assert run_mock.call_args.kwargs["cwd"] == "/srv/axion"


def test_relaunch_elevated_posix_propagates_exit_code(mocker) -> None:
    mocker.patch(
        "axion_wizard.privileges.current_invocation", return_value=("/usr/bin/python3", [])
    )
    mocker.patch(
        "axion_wizard.privileges.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=3),
    )
    assert priv.relaunch_elevated_posix() == 3


def test_relaunch_elevated_posix_without_sudo(mocker) -> None:
    mocker.patch(
        "axion_wizard.privileges.current_invocation", return_value=("/usr/bin/python3", [])
    )
    mocker.patch(
        "axion_wizard.privileges.subprocess.run", side_effect=FileNotFoundError("sudo")
    )
    with pytest.raises(priv.ElevationError, match="sudo"):
        priv.relaunch_elevated_posix()


def test_relaunch_elevated_dispatches_by_platform(mocker) -> None:
    mocker.patch("axion_wizard.privileges.is_windows", return_value=True)
    win = mocker.patch("axion_wizard.privileges.relaunch_elevated_windows", return_value=0)
    priv.relaunch_elevated()
    win.assert_called_once()

    mocker.patch("axion_wizard.privileges.is_windows", return_value=False)
    posix = mocker.patch("axion_wizard.privileges.relaunch_elevated_posix", return_value=0)
    priv.relaunch_elevated()
    posix.assert_called_once()


# --- opciones duplicadas al relanzar elevado -------------------------------------
#
# Regresión: `ensure_elevated` antepone `--project-dir <absoluto>` a los
# argumentos originales, que ya podían traer un `--project-dir` del usuario.
# Click se queda con la ÚLTIMA aparición de una opción no repetible, así que
# ganaba la del usuario — y si era relativa, el hijo la resolvía contra su
# propio directorio de trabajo y desplegaba el stack en el sitio equivocado.


def test_strip_overridden_options_removes_the_option_and_its_value() -> None:
    args = ["--project-dir", "./axion", "install", "--unattended"]
    assert priv.strip_overridden_options(args) == ["install", "--unattended"]


def test_strip_overridden_options_supports_the_equals_form() -> None:
    args = ["--project-dir=./axion", "install"]
    assert priv.strip_overridden_options(args) == ["install"]


def test_strip_overridden_options_leaves_everything_else_alone() -> None:
    args = ["--verbose", "install", "--config", "axion.toml"]
    assert priv.strip_overridden_options(args) == args


def test_current_invocation_never_passes_project_dir_twice(mocker) -> None:
    mocker.patch.object(priv.sys, "argv", ["axion-wizard", "--project-dir", "./axion", "install"])
    mocker.patch("axion_wizard.privileges.running_as_frozen_binary", return_value=True)

    _executable, args = priv.current_invocation(["--project-dir", "/abs/axion"])

    assert args.count("--project-dir") == 1
    assert args == ["--project-dir", "/abs/axion", "install"]


def test_current_invocation_keeps_other_args_in_dev_mode(mocker) -> None:
    mocker.patch.object(
        priv.sys, "argv", ["axion-wizard", "--verbose", "--project-dir=./axion", "install"]
    )
    mocker.patch("axion_wizard.privileges.running_as_frozen_binary", return_value=False)

    _executable, args = priv.current_invocation(["--project-dir", "/abs/axion"])

    assert args == ["-m", "axion_wizard", "--project-dir", "/abs/axion", "--verbose", "install"]
