#!/usr/bin/env bash
# Packages the axion-wizard ELF binary on Linux (§7). There is no
# cross-compilation: this script only runs in an `ubuntu-22.04` job (an older
# glibc, for forward compatibility — see §7.2).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# The environment is always built by `bootstrap.sh`, which knows how to do it
# with or without uv. This script used to call `uv sync` directly and died with
# "command not found" on any machine without uv installed.
./scripts/bootstrap.sh --no-run

VENV_PYTHON=".venv/bin/python"

# PyInstaller is a development dependency: `uv sync --group dev` already brings
# it, but on the pip path it has to be asked for separately.
if ! "$VENV_PYTHON" -c "import PyInstaller" >/dev/null 2>&1; then
    "$VENV_PYTHON" -m pip install --quiet --disable-pip-version-check pyinstaller
fi

"$VENV_PYTHON" -m PyInstaller build/axion-wizard.spec \
    --distpath dist --workpath build/work --noconfirm

BINARY="dist/axion-wizard-linux-x86_64"
if [ ! -f "$BINARY" ]; then
    echo "Build failed: $BINARY was not produced" >&2
    exit 1
fi

chmod +x "$BINARY"

# Only THIS binary's line is replaced; the rest are kept.
#
# With a bare `>>`, every build left one more line for the same file and
# `sha256sum -c` ended up validating against hashes from earlier builds, failing
# on all but the last. But overwriting the whole file is no good either: it
# erased the Windows .exe's hash, so anyone building both in the same copy was
# always left with just one — exactly what happens when compiling under WSL on a
# repo where the Windows build was already done. Filtering by filename gets both
# right.
#
# The character class is `[ *]`, not a plain space: `sha256sum` in binary mode —
# the default under Git Bash on Windows — writes `<hash> *name`, so a filter
# anchored on a space walks straight past its own earlier line and the duplicate
# it exists to prevent comes back.
NAME="$(basename "$BINARY")"
TMP_SUMS="$(mktemp)"
if [ -f dist/checksums.txt ]; then
    grep -v "[ *]${NAME}\$" dist/checksums.txt > "$TMP_SUMS" || true
fi
(cd dist && sha256sum "$NAME") >> "$TMP_SUMS"
sort -k2 "$TMP_SUMS" > dist/checksums.txt
rm -f "$TMP_SUMS"

echo "Build complete: $BINARY"
echo ""
echo "Mandatory post-build verification (§7.2): run this binary on a machine or"
echo "container with no Python installed and execute 'axion-wizard doctor'."
