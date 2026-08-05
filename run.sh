#!/usr/bin/env bash
# Start Dowser, setting up whatever is missing on first run.
#
# Safe to re-run: every step below checks the real state of the environment
# rather than assuming a previous run finished.
set -euo pipefail

cd "$(dirname "$0")"

VENV=".venv"
PY="$VENV/bin/python"

# --- virtualenv -------------------------------------------------------------

# A venv is only useful to us if it has pip. Debian and Ubuntu ship `venv`
# without `ensurepip` unless python3-venv is installed, which leaves behind an
# environment that looks fine but cannot install anything.
venv_usable() {
  [ -x "$PY" ] && "$PY" -m pip --version >/dev/null 2>&1
}

apt_hint() {
  echo "       On Debian/Ubuntu: sudo apt install python3-venv python3-pip" >&2
  echo "       (some systems name it for the version, e.g. python3.11-venv)" >&2
}

if [ ! -x "$PY" ]; then
  echo "==> Creating virtualenv"
  if ! python3 -m venv "$VENV"; then
    echo "ERROR: could not create a virtualenv." >&2
    apt_hint
    exit 1
  fi
fi

if ! venv_usable; then
  echo "==> Virtualenv has no pip; repairing"
  "$PY" -m ensurepip --upgrade >/dev/null 2>&1 || true
fi

if ! venv_usable; then
  # --clear empties and rebuilds the environment in place. Nothing but
  # installed packages lives there, and they are about to be reinstalled.
  echo "==> Rebuilding the virtualenv from scratch"
  python3 -m venv --clear "$VENV" || true
fi

if ! venv_usable; then
  echo "ERROR: the virtualenv still has no pip, so nothing can be installed." >&2
  apt_hint
  echo "       Then delete .venv and run this script again." >&2
  exit 1
fi

# --- python dependencies ----------------------------------------------------

# Checking imports rather than the venv's existence: a half-built venv from an
# interrupted install would otherwise sail through and fail at startup.
if ! "$PY" -c 'import fastapi, uvicorn, httpx, pydantic, yt_dlp, playwright' >/dev/null 2>&1; then
  echo "==> Installing Python dependencies"
  "$PY" -m pip install --quiet --upgrade pip
  "$PY" -m pip install --quiet -r requirements.txt
fi

# --- chromium for network capture -------------------------------------------

chromium_ready() {
  "$PY" - <<'PROBE' >/dev/null 2>&1
import os, sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    sys.exit(0 if os.path.exists(p.chromium.executable_path) else 1)
PROBE
}

if ! chromium_ready; then
  echo "==> Installing Chromium for network capture (this downloads ~150MB)"
  # --with-deps pulls the system libraries Chromium needs, which matters on a
  # bare server install. It needs root, so only ask for it when we have it.
  if [ "$(uname -s)" = "Linux" ] && [ "$(id -u)" = "0" ]; then
    "$PY" -m playwright install --with-deps chromium
  else
    "$PY" -m playwright install chromium || {
      echo "WARNING: Chromium install failed. Network capture will be unavailable." >&2
      echo "         On Linux try: sudo $PY -m playwright install --with-deps chromium" >&2
    }
  fi
fi

# --- ffmpeg -----------------------------------------------------------------

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "WARNING: ffmpeg is not on PATH — HLS/DASH downloads will fail." >&2
  echo "         macOS: brew install ffmpeg | Debian/Ubuntu: sudo apt install ffmpeg" >&2
fi

# --- go ---------------------------------------------------------------------

[ -f .env ] && set -a && . ./.env && set +a

exec "$PY" -m app.main
