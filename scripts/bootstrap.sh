#!/usr/bin/env bash
# Deja axion-wizard listo para usar en un solo comando (Linux / macOS / WSL).
#
# Busca un Python >= 3.11, crea el entorno virtual, instala las dependencias y
# arranca el wizard. Es idempotente: volver a ejecutarlo sobre un entorno ya
# montado solo actualiza lo que haga falta.
#
# Usa `uv` si está disponible (respeta uv.lock, así que el entorno es
# reproducible) y cae a `venv` + `pip` si no lo está. No instala uv por su
# cuenta: meter una herramienta global en la máquina de alguien sin pedirlo no
# es cosa de un script de arranque.
#
# Uso:
#   ./scripts/bootstrap.sh                 # instala todo y abre el wizard
#   ./scripts/bootstrap.sh --check         # instala + lint/tipos/tests
#   ./scripts/bootstrap.sh --no-run        # solo preparar el entorno
#   ./scripts/bootstrap.sh -- doctor       # instala y corre `axion-wizard doctor`
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

MIN_PYTHON_MINOR=11
VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"

RUN_CHECKS=0
RUN_WIZARD=1
WIZARD_ARGS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --check)  RUN_CHECKS=1; shift ;;
        --no-run) RUN_WIZARD=0; shift ;;
        --)       shift; WIZARD_ARGS=("$@"); break ;;
        *)        WIZARD_ARGS=("$@"); break ;;
    esac
done

if [ -t 1 ]; then
    C_CYAN='\033[36m'; C_GREEN='\033[32m'; C_YELLOW='\033[33m'; C_RED='\033[31m'; C_OFF='\033[0m'
else
    C_CYAN=''; C_GREEN=''; C_YELLOW=''; C_RED=''; C_OFF=''
fi

step() { printf "%b==> %s%b\n" "$C_CYAN" "$1" "$C_OFF"; }
ok()   { printf "%b    %s%b\n" "$C_GREEN" "$1" "$C_OFF"; }
warn() { printf "%b    %s%b\n" "$C_YELLOW" "$1" "$C_OFF"; }

# Un fallo de arranque tiene que decir qué pasó y qué hacer, no solo reventar:
# quien corre esto está montando el entorno por primera vez y no tiene contexto
# para interpretar un stack trace.
die() {
    local what="$1" why="$2"; shift 2
    printf "\n%bERROR: %s%b\n" "$C_RED" "$what" "$C_OFF" >&2
    printf "%b%s%b\n" "$C_RED" "$why" "$C_OFF" >&2
    if [ $# -gt 0 ]; then
        printf "\nQué hacer:\n" >&2
        local i=1
        for stepline in "$@"; do
            printf "  %d. %s\n" "$i" "$stepline" >&2
            i=$((i + 1))
        done
    fi
    printf "\n" >&2
    exit 1
}

run_checked() {
    local description="$1"; shift
    if ! "$@"; then
        die "$description falló" \
            "El entorno ha quedado a medias; el wizard no arrancaría correctamente." \
            "Reintentar el comando a mano para ver el error completo: $*"
    fi
}

python_is_recent_enough() {
    "$1" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, $MIN_PYTHON_MINOR) else 1)" \
        >/dev/null 2>&1
}

find_python() {
    local candidate
    for candidate in python3.14 python3.13 python3.12 python3.11 python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 && python_is_recent_enough "$candidate"; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

# --- 1. Intérprete --------------------------------------------------------------

step "Buscando Python >= 3.$MIN_PYTHON_MINOR"
if ! PYTHON="$(find_python)"; then
    die "No se encontró un Python 3.$MIN_PYTHON_MINOR o superior" \
        "axion-wizard usa sintaxis y librerías que no existen en versiones anteriores." \
        "Debian/Ubuntu: sudo apt install python3.12 python3.12-venv" \
        "Fedora: sudo dnf install python3.12" \
        "macOS: brew install python@3.12"
fi
ok "$PYTHON ($("$PYTHON" --version 2>&1))"

# --- 2. Entorno y dependencias ----------------------------------------------------

if command -v uv >/dev/null 2>&1; then
    step "Instalando dependencias con uv (respeta uv.lock)"
    run_checked "uv sync" uv sync --group dev
else
    warn "uv no está instalado; usando venv + pip (más lento y sin lockfile)."
    warn "Para builds reproducibles: curl -LsSf https://astral.sh/uv/install.sh | sh"

    if [ ! -x "$VENV_PYTHON" ]; then
        step "Creando el entorno virtual en .venv"
        if ! "$PYTHON" -m venv .venv; then
            die "No se pudo crear el entorno virtual" \
                "Sin él no hay dónde instalar las dependencias." \
                "En Debian/Ubuntu falta el paquete del módulo venv: sudo apt install python3-venv" \
                "Comprobar permisos de escritura en $PROJECT_ROOT"
        fi
    else
        ok "El entorno virtual ya existe."
    fi

    # Un .venv creado por uv no trae pip: uv instala paquetes él mismo y se lo
    # ahorra. Si este script cae al camino de pip sobre un entorno así (uv
    # instalado antes, ya no), `python -m pip` falla con "No module named pip"
    # y el arranque muere sin motivo aparente. `ensurepip` lo repone.
    if ! "$VENV_PYTHON" -m pip --version >/dev/null 2>&1; then
        step "El entorno no tiene pip (lo creó uv); reponiéndolo con ensurepip"
        if ! "$VENV_PYTHON" -m ensurepip --default-pip --upgrade >/dev/null 2>&1; then
            die "No se pudo reponer pip en el entorno virtual" \
                "Sin pip no hay forma de instalar las dependencias." \
                "Borrar la carpeta .venv y volver a ejecutar este script para crearla limpia."
        fi
    fi

    step "Instalando axion-wizard y sus dependencias"
    run_checked "pip install -e ." "$VENV_PYTHON" -m pip install --quiet --disable-pip-version-check -e .

    if [ "$RUN_CHECKS" -eq 1 ]; then
        # Las herramientas de desarrollo viven en [dependency-groups] (PEP 735),
        # que pip solo entiende desde 25.1 — se instalan por nombre para no
        # depender de la versión de pip que traiga el sistema.
        # `fastapi` y `python-multipart` no son dependencias del wizard: el
        # puente solo corre dentro del contenedor. Pero sus tests sí las
        # necesitan y, sin ellas, se saltan en silencio — que es peor que
        # fallar, porque `--check` diría "todo en verde" sin haber probado
        # el código que se envía al usuario.
        step "Instalando herramientas de desarrollo"
        run_checked "pip install (dev)" "$VENV_PYTHON" -m pip install --quiet --disable-pip-version-check \
            pytest pytest-cov pytest-mock ruff mypy fastapi python-multipart
    fi
fi

[ -x "$VENV_PYTHON" ] || die "El entorno virtual no quedó creado en .venv" \
    "Sin él no hay dónde ejecutar el wizard." \
    "Borrar la carpeta .venv y volver a ejecutar este script."
ok "Entorno listo: $VENV_PYTHON"

# --- 3. Verificación opcional -------------------------------------------------------

if [ "$RUN_CHECKS" -eq 1 ]; then
    step "Lint (ruff)"
    run_checked "ruff" "$VENV_PYTHON" -m ruff check .
    step "Tipos (mypy)"
    run_checked "mypy" "$VENV_PYTHON" -m mypy src
    step "Tests (pytest)"
    run_checked "pytest" "$VENV_PYTHON" -m pytest -q
    ok "Lint, tipos y tests en verde."
fi

# --- 4. Arranque ---------------------------------------------------------------------

if [ "$RUN_WIZARD" -eq 0 ]; then
    printf "\n"
    ok "Entorno preparado. Arrancar con:"
    printf "    .venv/bin/python -m axion_wizard --help\n"
    exit 0
fi

if [ ${#WIZARD_ARGS[@]} -eq 0 ]; then
    WIZARD_ARGS=(--help)
fi

step "Arrancando axion-wizard ${WIZARD_ARGS[*]}"
printf "\n"
exec "$VENV_PYTHON" -m axion_wizard "${WIZARD_ARGS[@]}"
