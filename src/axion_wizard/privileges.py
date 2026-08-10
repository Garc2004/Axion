"""Detecting and requesting elevated privileges.

The wizard touches things that require elevation depending on the platform:
`sysctl`/`ufw` and WireGuard's networking on Linux, and on Windows the
firewall, `netsh portproxy` and certain Docker Desktop operations.

A nuance against §9 of the spec, which asks for elevation *only* at the
points that require it: elevating the whole process up front is supported
here too, because asking for UAC halfway through an install is not viable on
Windows (an already-running process cannot be elevated; it would have to be
relaunched anyway, losing the interactive progress). What is kept from the
principle is that elevation is never silent: `explain_elevation_reason()`
says why before asking, and `--no-elevate` allows refusing.

A trade-off worth remembering: running elevated, the files the wizard writes
end up owned by root/Administrator.
"""

from __future__ import annotations

import ctypes
import os
import platform as _platform
import subprocess
import sys
from collections.abc import Sequence

ELEVATION_REASONS: tuple[str, ...] = (
    "apply the IP forwarding `sysctl` for WireGuard (Linux)",
    "open firewall ports (`ufw` on Linux, Defender on Windows)",
    "publish the service on the LAN (`netsh portproxy` under WSL2)",
)

# --- Windows API constants ----------------------------------------------------

#: `ShellExecuteExW` returns the process handle rather than closing it, so it
#: can be waited on.
SEE_MASK_NOCLOSEPROCESS = 0x00000040
#: Without this, `ShellExecuteExW` pumps window messages while starting the
#: child; in a console process with no message pump that can hang.
SEE_MASK_NOASYNC = 0x00000100
SW_SHOWNORMAL = 1
WAIT_OBJECT_0 = 0x00000000
INFINITE = 0xFFFFFFFF
#: `GetLastError()` after a `ShellExecuteExW` the user cancelled at the UAC
#: prompt.
ERROR_CANCELLED = 1223


class ElevationError(RuntimeError):
    """Privilege elevation could not be obtained or checked."""


def is_windows() -> bool:
    return _platform.system() == "Windows"


def is_elevated() -> bool:
    """`True` if the process runs as Administrator (Windows) or root (POSIX).

    Never raises: if it cannot be determined, "not elevated" is assumed, which
    is the conservative answer — at worst elevation is offered unnecessarily,
    never is a process that is not elevated taken to be.
    """
    if is_windows():
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            return False
    # `os.geteuid` does not exist on Windows, hence the getattr: that branch
    # already returned above, but the type checker analyses the whole module.
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return False
    return geteuid() == 0


def running_as_frozen_binary() -> bool:
    """`True` inside a PyInstaller bundle (§7)."""
    return bool(getattr(sys, "frozen", False))


#: Options `ensure_elevated` re-injects already resolved, and which therefore
#: have to be stripped from the original arguments so they are not passed
#: twice.
OVERRIDDEN_OPTIONS: tuple[str, ...] = ("--project-dir",)


def strip_overridden_options(
    args: Sequence[str], options: Sequence[str] = OVERRIDDEN_OPTIONS
) -> list[str]:
    """Strip `options` (and their values) from `args`.

    Without this, relaunching elevated passed `--project-dir` **twice**: the
    absolute path `ensure_elevated` computes and, after it, whatever the user
    typed on the original command line. Click keeps the *last* occurrence of a
    non-repeatable option, so the user's won — and if it was relative
    (`--project-dir ./axion`) the child resolved it against its own working
    directory, deploying the stack into `<project>/axion` instead of
    `<project>`. Exactly the failure passing an absolute path was meant to
    prevent.

    Supports both forms Click accepts: `--option value` and `--option=value`.
    """
    stripped: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in options:
            skip_next = True
            continue
        if any(arg.startswith(f"{option}=") for option in options):
            continue
        stripped.append(arg)
    return stripped


def current_invocation(leading_args: Sequence[str] = ()) -> tuple[str, list[str]]:
    """The executable and arguments to relaunch this very process with.

    These differ between the packaged binary (`axion-wizard.exe args…`) and
    development mode (`python -m axion_wizard args…`), because inside the
    bundle `sys.argv[0]` is already the executable itself rather than a script
    for the interpreter.

    `leading_args` is inserted *ahead of the wizard's arguments*, which is not
    the same as ahead of everything: in development mode, `-m axion_wizard`
    are the interpreter's arguments, and slipping `--project-dir` in front
    would have Python interpret it instead of the wizard.

    Options `leading_args` brings already resolved are stripped from the
    original arguments (see `strip_overridden_options`): duplicating them lets
    the user's unresolved one win, which is the opposite of the point of
    relaunching.
    """
    overridden = tuple(option for option in OVERRIDDEN_OPTIONS if option in leading_args)
    original_args = strip_overridden_options(sys.argv[1:], overridden)
    if running_as_frozen_binary():
        return sys.executable, [*leading_args, *original_args]
    return sys.executable, ["-m", "axion_wizard", *leading_args, *original_args]


def explain_elevation_reason() -> str:
    reasons = "\n".join(f"  - {reason}" for reason in ELEVATION_REASONS)
    return f"AXION needs administrator privileges in order to:\n{reasons}"


def _quote_windows_arg(arg: str) -> str:
    """Quote an argument for `ShellExecuteExW`, which receives its parameters
    as a single string rather than a list.

    This follows the rules of `CommandLineToArgvW`, which undoes it on the
    other side. The subtlety is backslashes: they only escape *in front of a
    quote*, so they have to be doubled there and left alone everywhere else.
    Without that, a path ending in a backslash
    (`--project-dir C:\\projects\\axion\\`) is quoted as
    `"C:\\projects\\axion\\"`, whose trailing backslash escapes the closing
    quote and swallows the rest of the command line.
    """
    if not arg:
        return '""'
    if not any(ch in arg for ch in ' \t"'):
        return arg

    quoted = ['"']
    backslashes = 0
    for char in arg:
        if char == "\\":
            backslashes += 1
            continue
        if char == '"':
            # The accumulated backslashes become escapes, and so does the quote.
            quoted.append("\\" * (backslashes * 2 + 1))
        else:
            quoted.append("\\" * backslashes)
        quoted.append(char)
        backslashes = 0
    # Trailing backslashes land immediately before the closing quote.
    quoted.append("\\" * (backslashes * 2))
    quoted.append('"')
    return "".join(quoted)


def _shellexecuteinfow_type() -> type[ctypes.Structure]:
    """Build the `SHELLEXECUTEINFOW` type.

    Done inside a function on purpose: `ctypes.wintypes` cannot even be
    imported off Windows (it fails defining `VARIANT_BOOL`), and this module
    is imported on every platform.
    """
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = (
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),  # hIcon/hMonitor union
            ("hProcess", wintypes.HANDLE),
        )

    return SHELLEXECUTEINFOW


def _start_elevated_process(executable: str, params: str, working_dir: str) -> int:
    """Trigger UAC and return the elevated process's handle (0 if none given).

    Isolates all the ctypes work so the policy above
    (`relaunch_elevated_windows`) is testable without touching the real API.
    """
    from ctypes import wintypes

    info_type = _shellexecuteinfow_type()
    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(info_type)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL

    info = info_type()
    info.cbSize = ctypes.sizeof(info_type)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.lpVerb = "runas"
    info.lpFile = executable
    info.lpParameters = params or None
    info.lpDirectory = working_dir
    info.nShow = SW_SHOWNORMAL

    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        code = int(ctypes.windll.kernel32.GetLastError())  # type: ignore[attr-defined]
        if code == ERROR_CANCELLED:
            raise ElevationError("the user cancelled the UAC prompt")
        raise ElevationError(f"ShellExecuteExW failed with code {code}")

    return int(info.hProcess or 0)


def _wait_for_process(handle: int) -> int:
    """Wait for `handle`'s process to finish and return its exit code."""
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL

    try:
        if int(kernel32.WaitForSingleObject(handle, INFINITE)) != WAIT_OBJECT_0:
            raise ElevationError("waiting on the elevated process failed")
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            # The process ran; we just do not know with what code. That is no
            # reason to declare the install failed.
            return 0
        return int(exit_code.value)
    finally:
        kernel32.CloseHandle(handle)


def relaunch_elevated_windows(
    leading_args: Sequence[str] = (), working_dir: str | None = None
) -> int:
    """Relaunch this process asking for UAC, **wait for it to finish**, and
    return its exit code.

    A Windows process cannot elevate itself: a new one has to be started with
    the `runas` verb, which is what triggers the UAC prompt. The child is also
    born with its own console — it cannot share the parent's, because they run
    at different integrity levels.

    That forces two things a bare `ShellExecuteW` cannot do:

    - **Wait for the child** (`SEE_MASK_NOCLOSEPROCESS` +
      `WaitForSingleObject`) and propagate its exit code. Without this the
      parent finished with 0 immediately: its window closed while the real
      work carried on in another, and neither the user nor a script ever
      learned whether it went well.
    - **Set the working directory** (`lpDirectory`). A process launched by
      UAC's AppInfo service does not inherit the parent's CWD: it starts in
      `C:\\Windows\\System32`. Since `--project-dir` defaults to
      `Path.cwd()`, the elevated child would have deployed the stack in there.

    `leading_args` goes before the original arguments rather than after: they
    are root-group options (`--project-dir`) and Click rejects them if they
    appear after the subcommand.
    """
    executable, args = current_invocation(leading_args)
    params = " ".join(_quote_windows_arg(a) for a in args)
    directory = working_dir if working_dir is not None else os.getcwd()

    try:
        handle = _start_elevated_process(executable, params, directory)
    except ElevationError:
        raise
    except (AttributeError, OSError, ValueError) as exc:
        raise ElevationError(f"could not invoke ShellExecuteExW: {exc}") from exc

    if not handle:
        # It started, but Windows returned no handle: there is nothing to
        # wait on.
        return 0

    try:
        return _wait_for_process(handle)
    except ElevationError:
        raise
    except (AttributeError, OSError, ValueError) as exc:
        raise ElevationError(f"could not wait on the elevated process: {exc}") from exc


def relaunch_elevated_posix(
    leading_args: Sequence[str] = (), working_dir: str | None = None
) -> int:
    """Re-exec this process under `sudo`, returning its exit code.

    Unlike Windows, this does chain within the same flow: `sudo` inherits the
    terminal and the working directory, so the user sees the password prompt
    and the wizard's output without changing window. `working_dir` is passed
    explicitly anyway so the behaviour does not depend on that inheritance.
    """
    executable, args = current_invocation(leading_args)
    command = ["sudo", "-E", executable, *args]
    try:
        completed = subprocess.run(command, shell=False, check=False, cwd=working_dir)
    except FileNotFoundError as exc:
        raise ElevationError("`sudo` is not available on this system") from exc
    return completed.returncode


def relaunch_elevated(leading_args: Sequence[str] = (), working_dir: str | None = None) -> int:
    if is_windows():
        return relaunch_elevated_windows(leading_args=leading_args, working_dir=working_dir)
    return relaunch_elevated_posix(leading_args=leading_args, working_dir=working_dir)
