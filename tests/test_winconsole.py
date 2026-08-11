import io
import os
import sys

import pytest

from axion_wizard.utils import winconsole


@pytest.fixture(autouse=True)
def _restore_pause_flag():
    """`disable_pause` touches module state; without this one test would infect the rest."""
    winconsole._pause_enabled = True
    yield
    winconsole._pause_enabled = True


class _FakeStdin(io.StringIO):
    def __init__(self, interactive: bool = True) -> None:
        super().__init__("\n")
        self._interactive = interactive

    def isatty(self) -> bool:
        return self._interactive


# --- owns_its_console -----------------------------------------------------------

AXION_EXE = os.path.normcase(r"C:\axion\dist\axion-wizard.exe")
CMD_EXE = os.path.normcase(r"C:\Windows\System32\cmd.exe")


def _console_with(mocker, pids, executables, own=AXION_EXE):
    """Simulate a console with `pids` attached, each with its own executable.

    Only functions of the module itself are substituted. Patching
    `winconsole.os.getpid` would fake `os.getpid` for the whole process — it is
    the real `os` module — including pytest's temp-directory factory.
    """
    mocker.patch("axion_wizard.utils.winconsole.console_process_ids", return_value=pids)
    mocker.patch("axion_wizard.utils.winconsole._own_executable", return_value=own)
    mocker.patch(
        "axion_wizard.utils.winconsole._executable_of",
        side_effect=lambda pid: executables.get(pid),
    )
    mocker.patch("axion_wizard.utils.winconsole.psutil.pid_exists", return_value=True)


def test_owns_console_with_the_pyinstaller_bootloader_pair(mocker) -> None:
    """Regression for the reported bug: in the packaged .exe there are ALWAYS
    two attached processes (--onefile's bootloader + the child running the
    code), so the previous condition (`GetConsoleProcessList() == 1`) was never
    true and the window kept closing on its own after a double click."""
    _console_with(mocker, [100, 101], {100: AXION_EXE, 101: AXION_EXE})
    assert winconsole.owns_its_console() is True


def test_owns_console_when_this_process_is_the_only_one(mocker) -> None:
    _console_with(mocker, [100], {100: AXION_EXE})
    assert winconsole.owns_its_console() is True


def test_does_not_own_console_when_a_shell_is_also_attached(mocker) -> None:
    """Lanzado desde cmd/PowerShell la consola sobrevive al proceso: pausar
    pausing there would only get in the way."""
    _console_with(mocker, [100, 101, 200], {100: AXION_EXE, 101: AXION_EXE, 200: CMD_EXE})
    assert winconsole.owns_its_console() is False


def test_does_not_own_console_when_a_process_cannot_be_identified(mocker) -> None:
    """A live but opaque attachment could be the shell: it is not assumed to be ours."""
    _console_with(mocker, [100, 200], {100: AXION_EXE, 200: None})
    assert winconsole.owns_its_console() is False


def test_ignores_a_process_that_died_between_the_call_and_the_lookup(mocker) -> None:
    _console_with(mocker, [100, 200], {100: AXION_EXE, 200: None})
    mocker.patch("axion_wizard.utils.winconsole.psutil.pid_exists", return_value=False)
    assert winconsole.owns_its_console() is True


def test_does_not_own_console_without_console_at_all(mocker) -> None:
    mocker.patch("axion_wizard.utils.winconsole.console_process_ids", return_value=[])
    assert winconsole.owns_its_console() is False


def test_console_process_ids_is_empty_outside_windows(mocker) -> None:
    mocker.patch.object(sys, "platform", "linux")
    assert winconsole.console_process_ids() == []


def test_console_process_ids_returns_the_attached_pids(mocker) -> None:
    mocker.patch.object(sys, "platform", "win32")
    fake = mocker.patch("axion_wizard.utils.winconsole.ctypes.windll", create=True)

    def fill(buffer, size):
        buffer[0], buffer[1] = 4242, 4243
        return 2

    fake.kernel32.GetConsoleProcessList.side_effect = fill
    assert winconsole.console_process_ids() == [4242, 4243]


def test_console_process_ids_never_raises_when_api_is_missing(mocker) -> None:
    mocker.patch.object(sys, "platform", "win32")
    fake = mocker.patch("axion_wizard.utils.winconsole.ctypes.windll", create=True)
    type(fake).kernel32 = mocker.PropertyMock(side_effect=OSError("sin kernel32"))
    assert winconsole.console_process_ids() == []


def test_owns_its_console_never_raises_on_this_machine() -> None:
    assert isinstance(winconsole.owns_its_console(), bool)


# --- should_pause ------------------------------------------------------------------


def test_should_pause_when_console_is_ours_and_stdin_is_a_tty(mocker, monkeypatch) -> None:
    monkeypatch.delenv(winconsole.NO_PAUSE_ENV_VAR, raising=False)
    mocker.patch("axion_wizard.utils.winconsole.owns_its_console", return_value=True)
    mocker.patch.object(sys, "stdin", _FakeStdin(interactive=True))
    assert winconsole.should_pause() is True


def test_should_not_pause_when_stdin_is_redirected(mocker, monkeypatch) -> None:
    """`axion-wizard doctor < /dev/null` or CI: pausing there would hang the process."""
    monkeypatch.delenv(winconsole.NO_PAUSE_ENV_VAR, raising=False)
    mocker.patch("axion_wizard.utils.winconsole.owns_its_console", return_value=True)
    mocker.patch.object(sys, "stdin", _FakeStdin(interactive=False))
    assert winconsole.should_pause() is False


def test_should_not_pause_when_env_var_disables_it(mocker, monkeypatch) -> None:
    monkeypatch.setenv(winconsole.NO_PAUSE_ENV_VAR, "1")
    mocker.patch("axion_wizard.utils.winconsole.owns_its_console", return_value=True)
    mocker.patch.object(sys, "stdin", _FakeStdin(interactive=True))
    assert winconsole.should_pause() is False


def test_should_not_pause_after_disable_pause(mocker, monkeypatch) -> None:
    monkeypatch.delenv(winconsole.NO_PAUSE_ENV_VAR, raising=False)
    mocker.patch("axion_wizard.utils.winconsole.owns_its_console", return_value=True)
    mocker.patch.object(sys, "stdin", _FakeStdin(interactive=True))
    winconsole.disable_pause()
    assert winconsole.should_pause() is False


def test_should_not_pause_when_stdin_is_none(mocker, monkeypatch) -> None:
    """In a bundle with no console, `sys.stdin` can be None."""
    monkeypatch.delenv(winconsole.NO_PAUSE_ENV_VAR, raising=False)
    mocker.patch("axion_wizard.utils.winconsole.owns_its_console", return_value=True)
    mocker.patch.object(sys, "stdin", None)
    assert winconsole.should_pause() is False


# --- pause_if_console_would_close ----------------------------------------------------


def test_pause_prompts_on_stderr_and_waits_for_a_line(mocker) -> None:
    """El prompt va a stderr para no contaminar un stdout redirigido."""
    mocker.patch("axion_wizard.utils.winconsole.should_pause", return_value=True)
    stdin = _FakeStdin()
    stderr = io.StringIO()
    mocker.patch.object(sys, "stdin", stdin)
    mocker.patch.object(sys, "stderr", stderr)

    winconsole.pause_if_console_would_close()

    assert "Enter" in stderr.getvalue()
    assert stdin.tell() > 0  # the line was consumed


def test_pause_does_nothing_when_not_needed(mocker) -> None:
    mocker.patch("axion_wizard.utils.winconsole.should_pause", return_value=False)
    stderr = io.StringIO()
    mocker.patch.object(sys, "stderr", stderr)

    winconsole.pause_if_console_would_close()

    assert stderr.getvalue() == ""


def test_pause_swallows_a_closed_stdin(mocker) -> None:
    """A stdin closed midway must not turn a clean exit into an error."""
    mocker.patch("axion_wizard.utils.winconsole.should_pause", return_value=True)
    stdin = _FakeStdin()
    stdin.close()
    mocker.patch.object(sys, "stdin", stdin)
    mocker.patch.object(sys, "stderr", io.StringIO())

    winconsole.pause_if_console_would_close()  # no lanza
