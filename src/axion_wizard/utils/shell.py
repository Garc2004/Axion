"""Subprocess seguro con streaming de salida.

Reglas de §6.3 de la spec:
- Siempre `subprocess.run([...], shell=False)` con lista de argumentos, nunca
  `os.system` ni cadenas armadas a mano.
- Para comandos que deben correr dentro de WSL desde Windows: prefijar con
  `["wsl.exe", "-d", distro, "--", ...]`.
- `encoding="utf-8", errors="replace"` siempre — la consola de Windows puede
  entregar cp1252 y hacer explotar el decode.
- Timeout explícito en toda invocación; ningún subprocess sin límite.
"""

from __future__ import annotations

import queue
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

DEFAULT_TIMEOUT = 30.0


class CommandNotFoundError(RuntimeError):
    """El ejecutable no existe en PATH (ni en el WSL de destino)."""


class CommandTimeoutError(RuntimeError):
    """El comando excedió el timeout permitido."""

    def __init__(self, command_args: Sequence[str], timeout: float) -> None:
        self.command_args = list(command_args)
        self.timeout = timeout
        super().__init__(f"timeout de {timeout}s excedido ejecutando: {' '.join(command_args)}")


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def wsl_prefix(args: Sequence[str], distro: str | None = None) -> list[str]:
    """Prefija `args` para que corran dentro de WSL desde un host Windows."""
    prefix = ["wsl.exe"]
    if distro:
        prefix += ["-d", distro]
    return [*prefix, "--", *args]


def run(
    args: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    check: bool = False,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> CommandResult:
    """Ejecuta un comando de forma segura y devuelve stdout/stderr decodificados.

    Nunca usa `shell=True`. Lanza `CommandNotFoundError` si el ejecutable no
    existe y `CommandTimeoutError` si excede `timeout`.
    """
    args = list(args)
    try:
        proc = subprocess.run(
            args,
            shell=False,
            timeout=timeout,
            cwd=cwd,
            env=env,
            input=input_text,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise CommandNotFoundError(f"ejecutable no encontrado: {args[0]!r}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CommandTimeoutError(args, timeout) from exc

    result = CommandResult(
        args=args,
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )
    if check and not result.ok:
        raise subprocess.CalledProcessError(
            result.returncode, args, output=result.stdout, stderr=result.stderr
        )
    return result


def run_streaming(
    args: Sequence[str],
    *,
    on_line: Callable[[str], None],
    timeout: float = 300.0,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> CommandResult:
    """Ejecuta un comando de larga duración, invocando `on_line` por cada línea
    de stdout+stderr combinados a medida que llegan (para barras de progreso)."""
    args = list(args)
    try:
        proc = subprocess.Popen(
            args,
            shell=False,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except FileNotFoundError as exc:
        raise CommandNotFoundError(f"ejecutable no encontrado: {args[0]!r}") from exc

    lines: list[str] = []
    finished_cleanly = False
    try:
        for line in _iter_lines_with_timeout(proc, args, timeout):
            lines.append(line)
            on_line(line.rstrip("\n"))
        finished_cleanly = True
    finally:
        _terminate(proc, expect_exit=finished_cleanly)

    return CommandResult(
        args=args,
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout="".join(lines),
        stderr="",
    )


_STREAM_EOF = object()


def _iter_lines_with_timeout(
    proc: subprocess.Popen, args: list[str], timeout: float
) -> Iterator[str]:
    """Itera las líneas de `proc.stdout` respetando un deadline global real.

    Iterar el pipe directamente (`for line in proc.stdout`) haría que el
    timeout solo pudiera comprobarse *entre* líneas: un proceso que se
    cuelga sin escribir nada bloquearía para siempre, que es justo lo que
    §6.3 prohíbe. Se lee en un hilo aparte y se espera en una cola con
    timeout, para poder abortar aunque no llegue una sola línea. Se usa un
    hilo (y no `selectors`) porque en Windows no se puede hacer `select()`
    sobre un pipe.
    """
    assert proc.stdout is not None
    line_queue: queue.Queue[object] = queue.Queue()

    def _reader() -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                line_queue.put(line)
        finally:
            line_queue.put(_STREAM_EOF)

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CommandTimeoutError(args, timeout)
        try:
            item = line_queue.get(timeout=remaining)
        except queue.Empty:
            raise CommandTimeoutError(args, timeout) from None
        if item is _STREAM_EOF:
            return
        yield item  # type: ignore[misc]


#: Margen para que un proceso que ya cerró stdout acabe de salir por su
#: cuenta antes de matarlo. Es la diferencia entre leer su código de salida
#: real y sustituirlo por uno de señal.
_GRACEFUL_EXIT_TIMEOUT = 5.0


def _terminate(proc: subprocess.Popen, *, expect_exit: bool = False) -> None:
    """Se asegura de que el proceso quede muerto y libera el pipe.

    Hay dos caminos, y confundirlos costaba el código de salida:

    - **Abortando** (`expect_exit=False`, p.ej. tras un timeout): primero
      `kill()`, después `close()`. Cerrar el pipe mientras el hilo lector
      sigue bloqueado en una lectura no lo interrumpe — `close()` se queda
      esperando a que esa lectura termine, así que un proceso colgado
      seguiría bloqueando aquí y el timeout no serviría de nada. Matando
      primero, el hijo cierra su extremo, el lector recibe EOF y `close()`
      retorna al instante.

    - **Terminación normal** (`expect_exit=True`, ya llegó el EOF del pipe):
      **esperar** antes de matar. El EOF llega cuando el hijo cierra stdout,
      que es parte de su salida pero no el final: durante esos microsegundos
      `poll()` todavía devuelve `None`. Matar ahí era una carrera contra un
      proceso que ya había terminado bien, y en Windows `TerminateProcess`
      sí llega a ganarla — el código real se perdía y quedaba un 1, con lo
      que un `docker compose up` correcto se reportaba como fallo de
      despliegue. Aquí ya no hay nada colgado que temer: el pipe está
      cerrado, así que esperar no puede bloquear indefinidamente.
    """
    if not expect_exit and proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=_GRACEFUL_EXIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    if proc.stdout is not None:
        try:
            proc.stdout.close()
        except OSError:
            pass
