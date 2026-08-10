"""Mantiene abierta la ventana de consola cuando el proceso es su dueño.

En Windows, un proceso al que se le *crea* la consola —doble clic desde el
Explorador, o el relanzamiento elevado por UAC— la pierde en el instante en
que termina: conhost destruye la ventana junto con el último proceso adjunto.
Desde fuera eso es indistinguible de un cierre inesperado: la ventana
parpadea y desaparece sin error, sin traceback y sin código de salida
visible, aunque el programa haya terminado perfectamente.

Solo se pausa si se cumplen las tres condiciones a la vez: la consola es
nuestra (`GetConsoleProcessList` devuelve 1), hay un humano al otro lado
(stdin es una TTY) y nadie lo ha desactivado (`AXION_NO_PAUSE`). Así ni CI
ni una tubería (`axion-wizard doctor | tee log.txt`) se quedan colgados
esperando un Enter que nunca va a llegar.
"""

from __future__ import annotations

import ctypes
import os
import sys

import psutil

#: Escape para desactivar la pausa desde fuera (CI, wrappers, scripts).
NO_PAUSE_ENV_VAR = "AXION_NO_PAUSE"

PAUSE_PROMPT = "\nPulsa Enter para cerrar esta ventana… "

#: Huecos del buffer de `GetConsoleProcessList`. Una consola normal tiene un
#: puñado de procesos adjuntos; 32 sobra y evita una segunda llamada.
_PROCESS_LIST_BUFFER_SIZE = 32

_pause_enabled = True


def disable_pause() -> None:
    """Desactiva la pausa para lo que reste de proceso.

    Lo usa el proceso padre tras relanzarse elevado: la ventana del hijo ya
    pausa por su cuenta, y pedir dos veces Enter en dos ventanas distintas
    para una sola ejecución es peor que no pausar.
    """
    global _pause_enabled
    _pause_enabled = False


def console_process_ids() -> list[int]:
    """PIDs adjuntos a nuestra consola; lista vacía si no hay o no se puede.

    Nunca lanza: sin consola, `GetConsoleProcessList` devuelve 0.
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
    """El ejecutable de este proceso.

    Función aparte, y sin `os.getpid()`, para que los tests puedan sustituirla
    sin tocar nada global: parchear `winconsole.os.getpid` falsea `os.getpid`
    para *todo* el proceso —`winconsole.os` es el módulo `os`— incluida la
    factoría de temporales de pytest, que lo usa para nombrar y bloquear sus
    directorios. Hacerlo disparaba la suite de 25 s a más de una hora.
    """
    try:
        return os.path.normcase(psutil.Process().exe() or "")
    except (psutil.Error, OSError, ValueError):
        return None


def owns_its_console() -> bool:
    """`True` si la consola es exclusivamente nuestra y morirá con nosotros.

    El criterio es que *todo* proceso adjunto corra el mismo ejecutable que
    nosotros. Si hay alguno distinto —`cmd.exe`, `powershell.exe`,
    `bash.exe`, `WindowsTerminal.exe`— es que nos escribieron desde un shell
    que sobrevive a nuestra salida, y ahí pausar solo estorba.

    **No basta con contar procesos.** La versión anterior comprobaba
    `GetConsoleProcessList() == 1`, que es el truco habitual, y en el binario
    distribuido no se cumplía nunca: PyInstaller `--onefile` arranca *dos*
    procesos —el bootloader, que descomprime el bundle en un temporal, y el
    hijo que ejecuta el código Python— y los dos quedan adjuntos a la misma
    consola. Un `.exe` empaquetado ve siempre 2 como mínimo, así que la pausa
    no llegaba a aplicarse jamás y la ventana seguía cerrándose sola.

    Comparar por ejecutable, y no contar, distingue esos dos procesos
    nuestros de un shell ajeno sin depender de cuántos sean.
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
            # Un proceso que ya murió entre la llamada y la consulta no dice
            # nada; uno que no podemos identificar, sí: podría ser el shell.
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
    """Las tres condiciones, en orden de coste creciente."""
    if not _pause_enabled:
        return False
    if os.environ.get(NO_PAUSE_ENV_VAR):
        return False
    if not _stdin_is_interactive():
        return False
    return owns_its_console()


def pause_if_console_would_close(prompt: str = PAUSE_PROMPT) -> None:
    """Espera un Enter antes de dejar morir la ventana.

    El prompt va a stderr, no a stdout: quien redirija la salida del wizard
    a un archivo no quiere este mensaje dentro (y si la redirige, tampoco
    llegamos aquí, porque stdin/stdout dejan de ser TTY).
    """
    if not should_pause():
        return
    try:
        sys.stderr.write(prompt)
        sys.stderr.flush()
        sys.stdin.readline()
    except (OSError, ValueError, EOFError, KeyboardInterrupt):
        pass  # Ctrl-C o stdin cerrado: cerrar sin más es exactamente lo pedido.
