#!/usr/bin/env bash
# Empaqueta el binario ELF de axion-wizard en Linux (§7). No hay
# cross-compilation: este script solo se corre en un job `ubuntu-22.04`
# (glibc más antigua, para compatibilidad hacia adelante — ver §7.2).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# El entorno lo monta siempre `bootstrap.sh`, que sabe hacerlo con uv o sin él.
# Antes este script llamaba a `uv sync` directamente y moría con "command not
# found" en cualquier máquina sin uv instalado.
./scripts/bootstrap.sh --no-run

VENV_PYTHON=".venv/bin/python"

# PyInstaller es dependencia de desarrollo: con `uv sync --group dev` ya está,
# pero por el camino de pip hay que pedirlo aparte.
if ! "$VENV_PYTHON" -c "import PyInstaller" >/dev/null 2>&1; then
    "$VENV_PYTHON" -m pip install --quiet --disable-pip-version-check pyinstaller
fi

"$VENV_PYTHON" -m PyInstaller build/axion-wizard.spec \
    --distpath dist --workpath build/work --noconfirm

BINARY="dist/axion-wizard-linux-x86_64"
if [ ! -f "$BINARY" ]; then
    echo "Build falló: no se generó $BINARY" >&2
    exit 1
fi

chmod +x "$BINARY"

# Se reemplaza la línea de ESTE binario y se conservan las demás.
#
# Con `>>` a secas, cada build dejaba una línea más para el mismo archivo y
# `sha256sum -c` acababa validando contra hashes de builds anteriores,
# fallando en todos menos el último. Pero sobrescribir el archivo entero
# tampoco vale: borraba el hash del .exe de Windows, así que quien
# construyera los dos en la misma copia se quedaba siempre con uno solo
# —justo lo que pasa al compilar en WSL sobre un repo donde ya se hizo el
# build de Windows—. Filtrar por nombre de archivo hace las dos cosas bien.
NAME="$(basename "$BINARY")"
TMP_SUMS="$(mktemp)"
if [ -f dist/checksums.txt ]; then
    grep -v " ${NAME}\$" dist/checksums.txt > "$TMP_SUMS" || true
fi
(cd dist && sha256sum "$NAME") >> "$TMP_SUMS"
sort -k2 "$TMP_SUMS" > dist/checksums.txt
rm -f "$TMP_SUMS"

echo "Build completo: $BINARY"
echo ""
echo "Verificación post-build obligatoria (§7.2): ejecutar este binario en una"
echo "máquina o contenedor sin Python instalado y correr 'axion-wizard doctor'."
