"""Safe subprocess execution with output streaming.

Rules from §6.3 of the spec:
- Always `subprocess.run([...], shell=False)` with an argument list, never
  `os.system` or hand-assembled strings.
- For commands that must run inside WSL from Windows: prefix with
  `["wsl.exe", "-d", distro, "--", ...]`.
- `encoding="utf-8", errors="replace"` always — the Windows console can hand
  back cp1252 and blow up the decode.
- An explicit timeout on every invocation; no subprocess without a limit.
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
    """The executable is not on PATH (nor in the target WSL distro)."""


class CommandTimeoutError(RuntimeError):
    """The command exceeded its allowed timeout."""

    def __init__(self, command_args: Sequence[str], timeout: float) -> None:
        self.command_args = list(command_args)
        self.timeout = timeout
        super().__init__(f"{timeout}s timeout exceeded running: {' '.join(command_args)}")


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
    """Prefix `args` so they run inside WSL from a Windows host."""
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
    """Run a command safely and return decoded stdout/stderr.

    Never uses `shell=True`. Raises `CommandNotFoundError` if the executable
    does not exist and `CommandTimeoutError` if it exceeds `timeout`.
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
        raise CommandNotFoundError(f"executable not found: {args[0]!r}") from exc
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
    """Run a long-lived command, invoking `on_line` for each line of combined
    stdout+stderr as it arrives (for progress bars)."""
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
        raise CommandNotFoundError(f"executable not found: {args[0]!r}") from exc

    lines: list[str] = []
    finished_cleanly = False
    reader, line_queue = _start_reader(proc)
    try:
        for line in _iter_lines_with_timeout(line_queue, args, timeout):
            lines.append(line)
            on_line(line.rstrip("\n"))
        finished_cleanly = True
    finally:
        _terminate(proc, reader=reader, expect_exit=finished_cleanly)

    return CommandResult(
        args=args,
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout="".join(lines),
        stderr="",
    )


_STREAM_EOF = object()


def _start_reader(proc: subprocess.Popen) -> tuple[threading.Thread, queue.Queue[object]]:
    """Start the thread that drains `proc.stdout` into a queue.

    A thread (rather than `selectors`) because on Windows you cannot `select()`
    on a pipe. It is started here, rather than inside
    `_iter_lines_with_timeout`, so `_terminate` can also reach it: whether that
    thread is still blocked on a read decides whether closing the pipe is safe
    — see `_terminate`.
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
    return reader, line_queue


def _iter_lines_with_timeout(
    line_queue: queue.Queue[object], args: list[str], timeout: float
) -> Iterator[str]:
    """Iterate the lines read by `_start_reader` against a real global deadline.

    Iterating the pipe directly (`for line in proc.stdout`) would only let the
    timeout be checked *between* lines: a process that hangs without writing
    anything would block forever, which is exactly what §6.3 forbids. Waiting
    on the queue with a timeout instead means the deadline holds even if not
    one line ever arrives.
    """
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


#: Grace period for a process that has already closed stdout to finish
#: exiting on its own before being killed. It is the difference between
#: reading its real exit code and replacing it with a signal's.
_GRACEFUL_EXIT_TIMEOUT = 5.0

#: How long to wait for the reader thread to notice the pipe is finished before
#: giving up on closing it. See `_terminate`.
_READER_JOIN_TIMEOUT = 5.0


def _terminate(
    proc: subprocess.Popen,
    *,
    reader: threading.Thread | None = None,
    expect_exit: bool = False,
) -> None:
    """Make sure the process is dead and release the pipe.

    There are two paths, and confusing them cost the exit code:

    - **Aborting** (`expect_exit=False`, e.g. after a timeout): `kill()`
      first, `close()` after. Closing the pipe while the reader thread is
      still blocked on a read does not interrupt it — `close()` waits for that
      read to finish, so a hung process would still block here and the timeout
      would achieve nothing. Killing first makes the child close its end, the
      reader gets EOF, and `close()` returns immediately.

    - **Normal termination** (`expect_exit=True`, the pipe already hit EOF):
      **wait** before killing. EOF arrives when the child closes stdout, which
      is part of its exit but not the end of it: for those microseconds
      `poll()` still returns `None`. Killing there was a race against a
      process that had already finished cleanly, and on Windows
      `TerminateProcess` does win it — the real code was lost and replaced by
      a 1, so a successful `docker compose up` was reported as a deployment
      failure. There is nothing hung to fear here: the pipe is closed, so
      waiting cannot block indefinitely.

    In both paths the `close()` itself is bounded, because "killing the child
    makes the reader get EOF" holds only while the child is the sole owner of
    the pipe's write end. On Windows a grandchild that inherited that handle
    keeps it open after its parent dies, the pending read never returns, and
    `close()` — which waits for exactly that read — blocks forever. That turns
    a timeout into a hang: the abort path is where the code has *already*
    decided to give up, and it would sit there instead, with the spinner still
    turning and nothing written to the state file. Rather than block, the handle
    is left to the garbage collector, which is the cheaper of the two leaks.
    """
    if not expect_exit and proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=_GRACEFUL_EXIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    if reader is not None:
        reader.join(timeout=_READER_JOIN_TIMEOUT)
        if reader.is_alive():
            # Still blocked on a read nobody is going to satisfy; closing here
            # would block with it.
            return
    if proc.stdout is not None:
        try:
            proc.stdout.close()
        except OSError:
            pass
