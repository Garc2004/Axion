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
    """Regresión: iterar el pipe directamente solo permitía comprobar el
    deadline *entre* líneas, así que un proceso que deja de escribir (un
    `docker compose up` colgado) bloqueaba indefinidamente en vez de
    abortar — §6.3 exige un límite real en toda invocación."""
    code = "import time; print('started', flush=True); time.sleep(30)"
    start = time.monotonic()

    with pytest.raises(shell.CommandTimeoutError):
        shell.run_streaming(
            [sys.executable, "-c", code], on_line=lambda _line: None, timeout=1.0
        )

    elapsed = time.monotonic() - start
    assert elapsed < 10, f"abortó en {elapsed:.1f}s; el timeout no está acotando la espera"


def test_run_streaming_times_out_when_no_output_at_all() -> None:
    code = "import time; time.sleep(30)"
    start = time.monotonic()

    with pytest.raises(shell.CommandTimeoutError):
        shell.run_streaming(
            [sys.executable, "-c", code], on_line=lambda _line: None, timeout=1.0
        )

    assert time.monotonic() - start < 10


def test_run_streaming_kills_the_process_on_timeout() -> None:
    """El proceso no debe quedar huérfano corriendo tras un timeout."""
    code = "import time; time.sleep(30)"
    procs_before = _child_python_count()

    with pytest.raises(shell.CommandTimeoutError):
        shell.run_streaming(
            [sys.executable, "-c", code], on_line=lambda _line: None, timeout=1.0
        )

    # Tras el timeout el hijo debe estar muerto, no acumulándose.
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


# --- terminación normal: esperar antes de matar ------------------------------------
#
# Regresión: `_terminate` hacía `kill()` en cuanto `poll()` devolvía None, y
# tras el EOF del pipe eso es una carrera contra un proceso que YA terminó
# bien — el EOF llega cuando el hijo cierra stdout, que es parte de su salida
# pero no el final. Perder esa carrera sustituye el código real por uno de
# señal, y un `docker compose up` correcto se reporta como fallo de despliegue.


@pytest.mark.parametrize("run", range(15))
def test_run_streaming_preserves_the_exit_code_after_eof(run: int) -> None:
    """Repetido: la ventana de la carrera es de microsegundos, así que una
    sola pasada no prueba gran cosa."""
    code = "import sys; print('linea'); sys.stdout.flush(); sys.exit(3)"
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
    proc.poll.return_value = None  # todavía no recogido
    proc.stdout = None

    shell._terminate(proc, expect_exit=True)

    proc.kill.assert_not_called()
    proc.wait.assert_called_once()


def test_terminate_kills_first_when_aborting(mocker) -> None:
    """El camino del timeout sí mata antes: cerrar el pipe con el lector
    bloqueado no lo interrumpe, así que un proceso colgado bloquearía aquí."""
    proc = mocker.Mock()
    proc.poll.return_value = None
    proc.stdout = None

    shell._terminate(proc, expect_exit=False)

    proc.kill.assert_called_once()
