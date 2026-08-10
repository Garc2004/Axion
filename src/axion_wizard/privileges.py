"""Detección y solicitud de privilegios elevados.

El wizard toca cosas que requieren elevación según la plataforma:
`sysctl`/`ufw` y la red de WireGuard en Linux, y en Windows el firewall,
`netsh portproxy` y ciertas operaciones de Docker Desktop.

Matiz respecto a §9 de la spec, que pide elevar *solo* en los puntos que lo
requieren: aquí se soporta además elevar el proceso entero de entrada,
porque pedir UAC a mitad de una instalación no es viable en Windows (no se
puede elevar un proceso ya arrancado; habría que relanzarlo igualmente y
perder el progreso interactivo). Lo que sí se conserva del principio es que
nunca se eleva en silencio: `explain_elevation_reason()` dice por qué antes
de pedirlo, y `--no-elevate` permite rechazarlo.

Contrapartida a tener presente: corriendo elevado, los archivos que el
wizard escribe quedan a nombre de root/Administrador.
"""

from __future__ import annotations

import ctypes
import os
import platform as _platform
import subprocess
import sys
from collections.abc import Sequence

ELEVATION_REASONS: tuple[str, ...] = (
    "aplicar `sysctl` de reenvío IP para WireGuard (Linux)",
    "abrir puertos en el firewall (`ufw` en Linux, Defender en Windows)",
    "publicar el servicio en la LAN (`netsh portproxy` bajo WSL2)",
)

# --- Constantes de la API de Windows ------------------------------------------

#: `ShellExecuteExW` devuelve el handle del proceso en vez de cerrarlo, para
#: poder esperarlo.
SEE_MASK_NOCLOSEPROCESS = 0x00000040
#: Sin esto, `ShellExecuteExW` procesa mensajes de ventana mientras arranca el
#: hijo; en un proceso de consola sin bomba de mensajes eso puede colgar.
SEE_MASK_NOASYNC = 0x00000100
SW_SHOWNORMAL = 1
WAIT_OBJECT_0 = 0x00000000
INFINITE = 0xFFFFFFFF
#: `GetLastError()` tras un `ShellExecuteExW` que el usuario canceló en UAC.
ERROR_CANCELLED = 1223


class ElevationError(RuntimeError):
    """No se pudo obtener o comprobar la elevación de privilegios."""


def is_windows() -> bool:
    return _platform.system() == "Windows"


def is_elevated() -> bool:
    """`True` si el proceso corre como Administrador (Windows) o root (POSIX).

    Nunca lanza: si no se puede determinar, se asume "sin elevar", que es la
    respuesta conservadora — como mucho se ofrecerá elevar de más, nunca se
    dará por elevado un proceso que no lo está.
    """
    if is_windows():
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            return False
    # `os.geteuid` no existe en Windows, de ahí el getattr: en esa rama ya se
    # ha devuelto arriba, pero el type checker analiza el módulo entero.
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return False
    return geteuid() == 0


def running_as_frozen_binary() -> bool:
    """`True` dentro de un bundle de PyInstaller (§7)."""
    return bool(getattr(sys, "frozen", False))


#: Opciones que `ensure_elevated` reinyecta ya resueltas y que, por tanto,
#: hay que quitar de los argumentos originales para no pasarlas dos veces.
OVERRIDDEN_OPTIONS: tuple[str, ...] = ("--project-dir",)


def strip_overridden_options(
    args: Sequence[str], options: Sequence[str] = OVERRIDDEN_OPTIONS
) -> list[str]:
    """Quita `options` (y sus valores) de `args`.

    Sin esto, relanzar elevado pasaba `--project-dir` **dos veces**: la ruta
    absoluta que calcula `ensure_elevated` y, detrás, la que el usuario
    escribió en la línea de comandos original. Click se queda con la
    *última* aparición de una opción no repetible, así que ganaba la del
    usuario — y si era relativa (`--project-dir ./axion`) el hijo la resolvía
    contra su propio directorio de trabajo, desplegando el stack en
    `<proyecto>/axion` en vez de en `<proyecto>`. Justo el fallo que pasar la
    ruta absoluta pretendía evitar.

    Soporta las dos formas que acepta Click: `--opcion valor` y
    `--opcion=valor`.
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
    """El ejecutable y los argumentos con los que relanzar este mismo proceso.

    Difiere entre el binario empaquetado (`axion-wizard.exe args...`) y el
    modo desarrollo (`python -m axion_wizard args...`), porque en el bundle
    `sys.argv[0]` ya es el propio ejecutable y no un script para el intérprete.

    `leading_args` se inserta *delante de los argumentos del wizard*, que no
    es lo mismo que delante de todo: en modo desarrollo, `-m axion_wizard`
    son argumentos del intérprete, y colar `--project-dir` antes haría que
    lo interpretara Python en vez del wizard.

    Las opciones que `leading_args` trae ya resueltas se eliminan de los
    argumentos originales (ver `strip_overridden_options`): duplicarlas hace
    que gane la del usuario, sin resolver, que es lo contrario de lo que se
    busca al relanzar.
    """
    overridden = tuple(option for option in OVERRIDDEN_OPTIONS if option in leading_args)
    original_args = strip_overridden_options(sys.argv[1:], overridden)
    if running_as_frozen_binary():
        return sys.executable, [*leading_args, *original_args]
    return sys.executable, ["-m", "axion_wizard", *leading_args, *original_args]


def explain_elevation_reason() -> str:
    reasons = "\n".join(f"  - {reason}" for reason in ELEVATION_REASONS)
    return f"AXION necesita privilegios de administrador para:\n{reasons}"


def _quote_windows_arg(arg: str) -> str:
    """Comilla un argumento para `ShellExecuteExW`, que recibe los parámetros
    como una sola cadena y no como lista.

    Sigue las reglas de `CommandLineToArgvW`, que es quien deshace esto al
    otro lado. La sutileza son las barras invertidas: solo son de escape
    *delante de una comilla*, así que hay que duplicarlas ahí y dejarlas tal
    cual en cualquier otra posición. Sin eso, una ruta acabada en barra
    (`--project-dir C:\\proyectos\\axion\\`) se comilla como
    `"C:\\proyectos\\axion\\"`, cuya barra final escapa la comilla de cierre
    y se lleva por delante el resto de la línea de comandos.
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
            # Las barras acumuladas pasan a ser de escape, y la comilla también.
            quoted.append("\\" * (backslashes * 2 + 1))
        else:
            quoted.append("\\" * backslashes)
        quoted.append(char)
        backslashes = 0
    # Las barras finales quedan justo antes de la comilla de cierre.
    quoted.append("\\" * (backslashes * 2))
    quoted.append('"')
    return "".join(quoted)


def _shellexecuteinfow_type() -> type[ctypes.Structure]:
    """Construye el tipo `SHELLEXECUTEINFOW`.

    Se hace dentro de una función a propósito: `ctypes.wintypes` ni siquiera
    se puede importar fuera de Windows (falla al definir `VARIANT_BOOL`), y
    este módulo se importa en todas las plataformas.
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
            ("hIcon", wintypes.HANDLE),  # unión hIcon/hMonitor
            ("hProcess", wintypes.HANDLE),
        )

    return SHELLEXECUTEINFOW


def _start_elevated_process(executable: str, params: str, working_dir: str) -> int:
    """Dispara UAC y devuelve el handle del proceso elevado (0 si no lo dio).

    Aísla toda la parte de ctypes para que la política de arriba
    (`relaunch_elevated_windows`) sea testeable sin tocar la API real.
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
            raise ElevationError("el usuario canceló el diálogo de UAC")
        raise ElevationError(f"ShellExecuteExW falló con código {code}")

    return int(info.hProcess or 0)


def _wait_for_process(handle: int) -> int:
    """Espera a que termine el proceso de `handle` y devuelve su código de salida."""
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL

    try:
        if int(kernel32.WaitForSingleObject(handle, INFINITE)) != WAIT_OBJECT_0:
            raise ElevationError("falló la espera al proceso elevado")
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            # El proceso corrió; solo no sabemos con qué código. No es motivo
            # para dar por fallida la instalación.
            return 0
        return int(exit_code.value)
    finally:
        kernel32.CloseHandle(handle)


def relaunch_elevated_windows(
    leading_args: Sequence[str] = (), working_dir: str | None = None
) -> int:
    """Relanza este proceso pidiendo UAC, **espera a que termine** y devuelve
    su código de salida.

    Un proceso Windows no puede auto-elevarse: hay que arrancar uno nuevo con
    el verbo `runas`, que es lo que dispara el diálogo de UAC. El hijo nace
    además con su propia consola — no puede compartir la del padre, porque
    tienen distinto nivel de integridad.

    Eso obliga a dos cosas que un `ShellExecuteW` a secas no puede hacer:

    - **Esperar al hijo** (`SEE_MASK_NOCLOSEPROCESS` + `WaitForSingleObject`)
      y propagar su código de salida. Sin esto el padre terminaba con 0 en el
      acto: su ventana se cerraba mientras el trabajo real seguía en otra, y
      ni el usuario ni un script llegaban a saber si había ido bien.
    - **Fijar el directorio de trabajo** (`lpDirectory`). Un proceso lanzado
      por el servicio AppInfo de UAC no hereda el CWD del padre: arranca en
      `C:\\Windows\\System32`. Como `--project-dir` cae por defecto en
      `Path.cwd()`, el hijo elevado habría desplegado el stack ahí dentro.

    `leading_args` se antepone a los argumentos originales, no se añade al
    final: son opciones del grupo raíz (`--project-dir`) y Click las rechaza
    si aparecen después del subcomando.
    """
    executable, args = current_invocation(leading_args)
    params = " ".join(_quote_windows_arg(a) for a in args)
    directory = working_dir if working_dir is not None else os.getcwd()

    try:
        handle = _start_elevated_process(executable, params, directory)
    except ElevationError:
        raise
    except (AttributeError, OSError, ValueError) as exc:
        raise ElevationError(f"no se pudo invocar ShellExecuteExW: {exc}") from exc

    if not handle:
        # Arrancó, pero Windows no devolvió handle: no hay a qué esperar.
        return 0

    try:
        return _wait_for_process(handle)
    except ElevationError:
        raise
    except (AttributeError, OSError, ValueError) as exc:
        raise ElevationError(f"no se pudo esperar al proceso elevado: {exc}") from exc


def relaunch_elevated_posix(
    leading_args: Sequence[str] = (), working_dir: str | None = None
) -> int:
    """Re-ejecuta este proceso bajo `sudo`, devolviendo su código de salida.

    A diferencia de Windows, aquí sí se encadena en el mismo flujo: `sudo`
    hereda la terminal y el directorio de trabajo, así que el usuario ve el
    prompt de contraseña y la salida del wizard sin cambiar de ventana.
    `working_dir` se pasa igualmente de forma explícita para que el
    comportamiento no dependa de esa herencia.
    """
    executable, args = current_invocation(leading_args)
    command = ["sudo", "-E", executable, *args]
    try:
        completed = subprocess.run(command, shell=False, check=False, cwd=working_dir)
    except FileNotFoundError as exc:
        raise ElevationError("`sudo` no está disponible en este sistema") from exc
    return completed.returncode


def relaunch_elevated(leading_args: Sequence[str] = (), working_dir: str | None = None) -> int:
    if is_windows():
        return relaunch_elevated_windows(leading_args=leading_args, working_dir=working_dir)
    return relaunch_elevated_posix(leading_args=leading_args, working_dir=working_dir)
