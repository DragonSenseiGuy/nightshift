#!/bin/bash
# Freeze the UI daemon into `dist/nightshiftd` (PyInstaller).
#
# Why a frozen binary at all: the shipped app has to run on a Mac with no Python, no `uv`
# and no checkout of this repo. `packaging/nightshiftd.py` is the entry point; this script
# is only the incantation, kept in a file because half of it is hidden-import bookkeeping
# that is impossible to remember and easy to get subtly wrong (a missing uvicorn submodule
# fails at *request* time, not at build time).
#
#   ./packaging/build_daemon.sh                 # dist/nightshiftd
#   NIGHTSHIFT_BUILD_PY=/path/to/python ./packaging/build_daemon.sh
#
# **Architecture is decided here, not by the .app.** PyInstaller does not cross-compile: it
# emits a binary for the interpreter that runs it. This checkout's `uv` (and therefore its
# `.venv`) is an x86_64 build, so freezing with it on an Apple Silicon Mac produces an
# Intel helper that a native arm64 app cannot spawn unless Rosetta happens to be installed
# — a demo that dies at launch on a clean M-series machine. So the script prefers a
# dedicated arm64 build environment and creates one from Homebrew's python3 if it is
# missing. `file dist/nightshiftd` is the check that matters after any change here.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
BUILD_VENV="build/arm64venv"

if [[ -n "${NIGHTSHIFT_BUILD_PY:-}" ]]; then
    PY="$NIGHTSHIFT_BUILD_PY"
elif [[ -x "$BUILD_VENV/bin/python" ]]; then
    PY="$BUILD_VENV/bin/python"
elif [[ -x /opt/homebrew/bin/python3 ]]; then
    echo "Creating the native build environment in $BUILD_VENV …"
    /opt/homebrew/bin/python3 -m venv "$BUILD_VENV"
    "$BUILD_VENV/bin/pip" install -q --disable-pip-version-check \
        fastapi uvicorn pydantic httpx python-dotenv keyring \
        google-auth google-auth-oauthlib google-api-python-client openai docker pyinstaller
    PY="$BUILD_VENV/bin/python"
else
    # No native interpreter available: fall back to the project venv and say what that
    # means, rather than silently shipping a binary half the Macs cannot run.
    echo "warning: no native python found; freezing with the project venv" >&2
    PY=".venv/bin/python"
fi

echo "Freezing with $PY ($("$PY" -c 'import platform; print(platform.machine())'))"

# The daemon imports fastapi/uvicorn lazily and pydantic dynamically, so PyInstaller's
# static analysis needs help: `--collect-all` pulls a package's submodules and data files
# in wholesale, which is the difference between "starts" and "starts, then 500s".
"$PY" -m PyInstaller \
    --noconfirm \
    --clean \
    --onefile \
    --name nightshiftd \
    --distpath dist \
    --workpath build/pyinstaller \
    --specpath build/pyinstaller \
    --paths "$ROOT" \
    --collect-all uvicorn \
    --collect-all fastapi \
    --collect-all pydantic \
    --collect-all starlette \
    --collect-submodules app \
    --collect-submodules orchestrator \
    --collect-submodules runner \
    --collect-submodules fixtures \
    --hidden-import config \
    --hidden-import approvals \
    --hidden-import transcripts \
    --hidden-import briefing \
    --hidden-import models \
    --hidden-import snapshots \
    --hidden-import gitops \
    --add-data "$ROOT/config/standing_instructions.toml:config" \
    packaging/nightshiftd.py

echo "Built dist/nightshiftd — $(file -b dist/nightshiftd)"
dist/nightshiftd --help >/dev/null && echo "Smoke test: --help ok"
