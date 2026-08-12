import sys
import time

import pytest

from axion_wizard.utils import shell


def test_run_ok_captures_stdout() -> None:
    result = shell.run([sys.executable, "-c", "print('hello')"])
    assert result.ok is True
    assert "hello" in result.stdout


def test_run_nonzero_exit_is_not_ok() -> None:
    result = shell.run([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert result.ok is False
    assert result.returncode == 3


def test_run_check_raises_on_nonzero_exit() -> None:
    import subprocess

    with pytest.raises(subprocess.CalledProcessError):
        shell.run([sys.executable, "-c", "import sys; sys.exit(1)"], check=True)


def test_run_command_not_found() -> None:
    with pytest.raises(shell.CommandNotFoundError):
        shell.run(["definitely-not-a-real-executable-xyz"])


def test_run_timeout() -> None:
    with pytest.raises(shell.CommandTimeoutError):
        shell.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)


def test_wsl_prefix_without_distro() -> None:
    assert shell.wsl_prefix(["ls", "-la"]) == ["wsl.exe", "--", "ls", "-la"]


def test_wsl_prefix_with_distro() -> None:
    assert shell.wsl_prefix(["ls"], distro="Ubuntu") == ["wsl.exe", "-d", "Ubuntu", "--", "ls"]


def test_run_streaming_collects_lines_in_order() -> None:
    lines: list[str] = []
    code = "print('one'); print('two'); print('three')"
    result = shell.run_streaming([sys.executable, "-c", code], on_line=lines.append)
    assert lines == ["one", "two", "three"]
    assert result.ok is True


def test_run_streaming_command_not_found() -> None:
    with pytest.raises(shell.CommandNotFoundError):
        shell.run_streaming(["definitely-not-a-real-executable-xyz"], on_line=lambda _line: None)


def test_run_streaming_captures_nonzero_exit() -> None:
    code = "import sys; print('before'); sys.exit(2)"
    lines: list[str] = []
    result = shell.run_streaming([sys.executable, "-c", code], on_line=lines.append)
    assert result.returncode == 2
    assert lines == ["before"]


def test_run_streaming_times_out_on_a_silent_process() -> None:
    """Regression: iterating the pipe directly only allowed checking the
    deadline *between* lines, so a process that stops writing (a hung
    `docker compose up`) blocked indefinitely rather than aborting — §6.3
    requires a real limit on every invocation."""
    code = "import time; print('started', flush=True); time.sleep(30)"
    start = time.monotonic()

    with pytest.raises(shell.CommandTimeoutError):
        shell.run_streaming(
            [sys.executable, "-c", code], on_line=lambda _line: None, timeout=1.0
        )

    elapsed = time.monotonic() - start
    assert elapsed < 10, f"aborted in {elapsed:.1f}s; the timeout is not bounding the wait"


def test_run_streaming_times_out_when_no_output_at_all() -> None:
    code = "import time; time.sleep(30)"
    start = time.monotonic()

    with pytest.raises(shell.CommandTimeoutError):
        shell.run_streaming(
            [sys.executable, "-c", code], on_line=lambda _line: None, timeout=1.0
        )

    assert time.monotonic() - start < 10


def test_run_streaming_kills_the_process_on_timeout() -> None:
    """The process must not be left orphaned and running after a timeout."""
    code = "import time; time.sleep(30)"
    procs_before = _child_python_count()

    with pytest.raises(shell.CommandTimeoutError):
        shell.run_streaming(
            [sys.executable, "-c", code], on_line=lambda _line: None, timeout=1.0
        )

    # After the timeout the child must be dead, not piling up.
    assert _child_python_count() <= procs_before


def _child_python_count() -> int:
    import psutil

    current = psutil.Process()
    return len([c for c in current.children(recursive=True) if c.is_running()])


def test_run_streaming_timeout_error_reports_the_command() -> None:
    code = "import time; time.sleep(30)"
    with pytest.raises(shell.CommandTimeoutError) as exc_info:
        shell.run_streaming(
            [sys.executable, "-c", code], on_line=lambda _line: None, timeout=0.5
        )
    assert exc_info.value.timeout == 0.5
    assert sys.executable in exc_info.value.command_args


# --- normal termination: wait before killing ---------------------------------------
#
# Regression: `_terminate` called `kill()` as soon as `poll()` returned None,
# and after the pipe's EOF that is a race against a process that has ALREADY
# finished cleanly — EOF arrives when the child closes stdout, which is part of
# its exit but not the end of it. Losing that race replaces the real exit code
# with a signal's, and a successful `docker compose up` gets reported as a
# deployment failure.


@pytest.mark.parametrize("run", range(15))
def test_run_streaming_preserves_the_exit_code_after_eof(run: int) -> None:
    """Repeated: the race window is microseconds wide, so a single pass does
    not prove much."""
    code = "import sys; print('line'); sys.stdout.flush(); sys.exit(3)"
    result = shell.run_streaming([sys.executable, "-c", code], on_line=lambda _line: None)
    assert result.returncode == 3


@pytest.mark.parametrize("run", range(10))
def test_run_streaming_reports_success_after_eof(run: int) -> None:
    code = "print('ok')"
    result = shell.run_streaming([sys.executable, "-c", code], on_line=lambda _line: None)
    assert result.ok is True
    assert result.returncode == 0


def test_terminate_does_not_kill_a_process_that_already_finished(mocker) -> None:
    proc = mocker.Mock()
    proc.poll.return_value = None  # not reaped yet
    proc.stdout = None

    shell._terminate(proc, expect_exit=True)

    proc.kill.assert_not_called()
    proc.wait.assert_called_once()


def test_terminate_kills_first_when_aborting(mocker) -> None:
    """The timeout path does kill first: closing the pipe while the reader is
    blocked does not interrupt it, so a hung process would block here."""
    proc = mocker.Mock()
    proc.poll.return_value = None
    proc.stdout = None

    shell._terminate(proc, expect_exit=False)

    proc.kill.assert_called_once()


# --- the bounded close ---------------------------------------------------------
#
# Regression: `_terminate` closed stdout unconditionally, on the assumption that
# killing the child guarantees the reader hits EOF. That only holds while the
# child owns the pipe's write end alone — on Windows a grandchild that inherited
# the handle keeps it open, the pending read never returns, and `close()` waits
# for exactly that read. A timeout then becomes a hang, in the one code path
# whose entire job is to give up.


def test_terminate_does_not_close_the_pipe_while_the_reader_is_stuck(mocker) -> None:
    mocker.patch.object(shell, "_READER_JOIN_TIMEOUT", 0.05)
    proc = mocker.Mock()
    proc.poll.return_value = None
    proc.stdout = mocker.Mock()
    stuck_reader = mocker.Mock()
    stuck_reader.is_alive.return_value = True

    shell._terminate(proc, reader=stuck_reader, expect_exit=False)

    stuck_reader.join.assert_called_once()
    proc.stdout.close.assert_not_called()


def test_terminate_closes_the_pipe_once_the_reader_is_done(mocker) -> None:
    proc = mocker.Mock()
    proc.poll.return_value = None
    proc.stdout = mocker.Mock()
    finished_reader = mocker.Mock()
    finished_reader.is_alive.return_value = False

    shell._terminate(proc, reader=finished_reader, expect_exit=False)

    proc.stdout.close.assert_called_once()


def test_run_streaming_returns_promptly_when_a_grandchild_holds_the_pipe() -> None:
    """The real shape of the bug, end to end: the child spawns a grandchild that
    inherits stdout and outlives it, so the pipe never reaches EOF. The timeout
    has to still return, rather than blocking in `_terminate`."""
    code = (
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
        "stdout=sys.stdout); "
        "print('spawned', flush=True); "
        "sys.exit(0)"
    )
    start = time.monotonic()

    with pytest.raises(shell.CommandTimeoutError):
        shell.run_streaming(
            [sys.executable, "-c", code], on_line=lambda _line: None, timeout=1.0
        )

    elapsed = time.monotonic() - start
    assert elapsed < 15, f"took {elapsed:.1f}s; _terminate is blocking on the orphaned pipe"
