"""Keep the console window open when this process owns it.

On Windows, a process that has a console *created* for it — double-clicked
from Explorer, or relaunched elevated by UAC — loses it the instant it exits:
conhost destroys the window along with the last attached process. From the
outside that is indistinguishable from a crash: the window blinks and
vanishes with no error, no traceback and no visible exit code, even though
the program finished perfectly.

The pause only happens when all three conditions hold at once: the console is
ours, there is a human on the other end (stdin is a TTY), and nobody has
turned it off (`AXION_NO_PAUSE`). That way neither CI nor a pipe
(`axion-wizard doctor | tee log.txt`) hangs waiting on an Enter that will
never come.
"""

from __future__ import annotations

import ctypes
import os
import sys

import psutil

#: Escape hatch to disable the pause from outside (CI, wrappers, scripts).
NO_PAUSE_ENV_VAR = "AXION_NO_PAUSE"

PAUSE_PROMPT = "\nPress Enter to close this window… "

#: Slots in the `GetConsoleProcessList` buffer. A normal console has a
#: handful of attached processes; 32 is plenty and avoids a second call.
_PROCESS_LIST_BUFFER_SIZE = 32

_pause_enabled = True


def disable_pause() -> None:
    """Disable the pause for the rest of this process.

    Used by the parent after relaunching elevated: the child's window already
    pauses on its own, and asking for Enter twice in two different windows for
    a single run is worse than not pausing at all.
    """
    global _pause_enabled
    _pause_enabled = False


def console_process_ids() -> list[int]:
    """PIDs attached to our console; empty list if there is none or it fails.

    Never raises: with no console, `GetConsoleProcessList` returns 0.
    """
    if sys.platform != "win32":
        return []
    try:
        buffer = (ctypes.c_uint32 * _PROCESS_LIST_BUFFER_SIZE)()
        attached = int(
            ctypes.windll.kernel32.GetConsoleProcessList(  # type: ignore[attr-defined]
                buffer, _PROCESS_LIST_BUFFER_SIZE
            )
        )
    except (AttributeError, OSError, ValueError):
        return []
    if attached <= 0:
        return []
    return list(buffer)[: min(attached, _PROCESS_LIST_BUFFER_SIZE)]


def _executable_of(pid: int) -> str | None:
    try:
        return os.path.normcase(psutil.Process(pid).exe() or "")
    except (psutil.Error, OSError, ValueError):
        return None


def _own_executable() -> str | None:
    """This process's own executable.

    A separate function, and without `os.getpid()`, so tests can substitute it
    without touching anything global: patching `winconsole.os.getpid` fakes
    `os.getpid` for the *whole* process — `winconsole.os` is the `os` module —
    including pytest's temp-directory factory, which uses it to name and lock
    its directories. Doing so blew the 25-second suite out to over an hour.
    """
    try:
        return os.path.normcase(psutil.Process().exe() or "")
    except (psutil.Error, OSError, ValueError):
        return None


def owns_its_console() -> bool:
    """`True` if the console is exclusively ours and will die with us.

    The criterion is that *every* attached process runs the same executable we
    do. If any of them differs — `cmd.exe`, `powershell.exe`, `bash.exe`,
    `WindowsTerminal.exe` — we were invoked from a shell that outlives our
    exit, and pausing there only gets in the way.

    **Counting processes is not enough.** The previous version checked
    `GetConsoleProcessList() == 1`, which is the usual trick, and in the
    distributed binary it was never true: PyInstaller `--onefile` starts *two*
    processes — the bootloader, which unpacks the bundle into a temp dir, and
    the child that runs the Python code — and both stay attached to the same
    console. A packaged `.exe` always sees at least 2, so the pause never
    applied and the window kept closing on its own.

    Comparing by executable rather than counting tells those two processes of
    ours apart from a foreign shell without depending on how many there are.
    """
    pids = console_process_ids()
    if not pids:
        return False

    own_executable = _own_executable()
    if own_executable is None:
        return False

    for pid in pids:
        executable = _executable_of(pid)
        if executable is None:
            # A process that died between the call and the query tells us
            # nothing; one we cannot identify does: it could be the shell.
            if psutil.pid_exists(pid):
                return False
            continue
        if executable != own_executable:
            return False
    return True


def _stdin_is_interactive() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, OSError, ValueError):
        return False


def should_pause() -> bool:
    """The three conditions, in order of increasing cost."""
    if not _pause_enabled:
        return False
    if os.environ.get(NO_PAUSE_ENV_VAR):
        return False
    if not _stdin_is_interactive():
        return False
    return owns_its_console()


def pause_if_console_would_close(prompt: str = PAUSE_PROMPT) -> None:
    """Wait for an Enter before letting the window die.

    The prompt goes to stderr, not stdout: anyone redirecting the wizard's
    output to a file does not want this message inside it (and if they do
    redirect, we never get here anyway, because stdin/stdout stop being TTYs).
    """
    if not should_pause():
        return
    try:
        sys.stderr.write(prompt)
        sys.stderr.flush()
        sys.stdin.readline()
    except (OSError, ValueError, EOFError, KeyboardInterrupt):
        pass  # Ctrl-C or closed stdin: just closing is exactly what was asked.
