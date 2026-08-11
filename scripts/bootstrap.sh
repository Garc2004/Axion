#!/usr/bin/env bash
# Gets axion-wizard ready to use in a single command (Linux / macOS / WSL).
#
# Looks for a Python >= 3.11, creates the virtual environment, installs the
# dependencies and starts the wizard. It is idempotent: running it again over an
# environment that is already set up only updates what needs updating.
#
# Uses `uv` if available (it honours uv.lock, so the environment is
# reproducible) and falls back to `venv` + `pip` if not. It does not install uv
# on its own: putting a global tool on someone's machine unasked is not a
# bootstrap script's business.
#
# Usage:
#   ./scripts/bootstrap.sh                 # install everything and open the wizard
#   ./scripts/bootstrap.sh --check         # install + lint/types/tests
#   ./scripts/bootstrap.sh --no-run        # only prepare the environment
#   ./scripts/bootstrap.sh -- doctor       # install and run `axion-wizard doctor`
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

# A bootstrap failure has to say what happened and what to do, not just blow up:
# whoever runs this is setting the environment up for the first time and has no
# context with which to read a stack trace.
die() {
    local what="$1" why="$2"; shift 2
    printf "\n%bERROR: %s%b\n" "$C_RED" "$what" "$C_OFF" >&2
    printf "%b%s%b\n" "$C_RED" "$why" "$C_OFF" >&2
    if [ $# -gt 0 ]; then
        printf "\nWhat to do:\n" >&2
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
        die "$description failed" \
            "The environment is half-built; the wizard would not start correctly." \
            "Retry the command by hand to see the full error: $*"
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

# --- 1. Interpreter -------------------------------------------------------------

step "Looking for Python >= 3.$MIN_PYTHON_MINOR"
if ! PYTHON="$(find_python)"; then
    die "No Python 3.$MIN_PYTHON_MINOR or newer was found" \
        "axion-wizard uses syntax and libraries that do not exist in earlier versions." \
        "Debian/Ubuntu: sudo apt install python3.12 python3.12-venv" \
        "Fedora: sudo dnf install python3.12" \
        "macOS: brew install python@3.12"
fi
ok "$PYTHON ($("$PYTHON" --version 2>&1))"

# --- 2. Environment and dependencies ----------------------------------------------

if command -v uv >/dev/null 2>&1; then
    step "Installing dependencies with uv (honours uv.lock)"
    run_checked "uv sync" uv sync --group dev
else
    warn "uv is not installed; using venv + pip (slower and without a lockfile)."
    warn "For reproducible builds: curl -LsSf https://astral.sh/uv/install.sh | sh"

    if [ ! -x "$VENV_PYTHON" ]; then
        step "Creating the virtual environment in .venv"
        if ! "$PYTHON" -m venv .venv; then
            die "The virtual environment could not be created" \
                "Without it there is nowhere to install the dependencies." \
                "On Debian/Ubuntu the venv module's package is missing: sudo apt install python3-venv" \
                "Check write permissions on $PROJECT_ROOT"
        fi
    else
        ok "The virtual environment already exists."
    fi

    # A .venv created by uv has no pip: uv installs packages itself and spares
    # itself the trouble. If this script falls back to the pip path over such an
    # environment (uv installed earlier, no longer), `python -m pip` fails with
    # "No module named pip" and the bootstrap dies for no visible reason.
    # `ensurepip` puts it back.
    if ! "$VENV_PYTHON" -m pip --version >/dev/null 2>&1; then
        step "The environment has no pip (uv created it); restoring it with ensurepip"
        if ! "$VENV_PYTHON" -m ensurepip --default-pip --upgrade >/dev/null 2>&1; then
            die "pip could not be restored in the virtual environment" \
                "Without pip there is no way to install the dependencies." \
                "Delete the .venv folder and run this script again to create it clean."
        fi
    fi

    step "Installing axion-wizard and its dependencies"
    run_checked "pip install -e ." "$VENV_PYTHON" -m pip install --quiet --disable-pip-version-check -e .

    if [ "$RUN_CHECKS" -eq 1 ]; then
        # The development tools live in [dependency-groups] (PEP 735), which pip
        # only understands from 25.1 onwards — they are installed by name so as
        # not to depend on whatever pip version the system ships.
        # `fastapi` and `python-multipart` are not dependencies of the wizard:
        # the bridge only runs inside the container. But its tests do need them
        # and, without them, they are skipped silently — which is worse than
        # failing, because `--check` would say "all green" without having tested
        # the code that gets shipped to the user.
        step "Installing development tools"
        run_checked "pip install (dev)" "$VENV_PYTHON" -m pip install --quiet --disable-pip-version-check \
            pytest pytest-cov pytest-mock ruff mypy fastapi python-multipart
    fi
fi

[ -x "$VENV_PYTHON" ] || die "The virtual environment was not created in .venv" \
    "Without it there is nowhere to run the wizard." \
    "Delete the .venv folder and run this script again."
ok "Environment ready: $VENV_PYTHON"

# --- 3. Optional verification -------------------------------------------------------

if [ "$RUN_CHECKS" -eq 1 ]; then
    step "Lint (ruff)"
    run_checked "ruff" "$VENV_PYTHON" -m ruff check .
    step "Types (mypy)"
    run_checked "mypy" "$VENV_PYTHON" -m mypy src
    step "Tests (pytest)"
    run_checked "pytest" "$VENV_PYTHON" -m pytest -q
    ok "Lint, types and tests all green."
fi

# --- 4. Start ---------------------------------------------------------------------

if [ "$RUN_WIZARD" -eq 0 ]; then
    printf "\n"
    ok "Environment prepared. Start it with:"
    printf "    .venv/bin/python -m axion_wizard --help\n"
    exit 0
fi

if [ ${#WIZARD_ARGS[@]} -eq 0 ]; then
    WIZARD_ARGS=(--help)
fi

step "Starting axion-wizard ${WIZARD_ARGS[*]}"
printf "\n"
exec "$VENV_PYTHON" -m axion_wizard "${WIZARD_ARGS[@]}"
