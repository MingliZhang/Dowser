#!/usr/bin/env bash
# Start Dowser, setting up the virtualenv on first run.
set -euo pipefail

cd "$(dirname "$0")"

VENV=".venv"
PY="$VENV/bin/python"

if [ ! -x "$PY" ]; then
  echo "==> Creating virtualenv"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --upgrade pip
  "$VENV/bin/pip" install -r requirements.txt
  echo "==> Installing Chromium for network capture"
  "$PY" -m playwright install chromium
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "WARNING: ffmpeg is not on PATH — HLS/DASH downloads will fail."
  echo "         macOS: brew install ffmpeg | Debian/Ubuntu: sudo apt install ffmpeg"
fi

[ -f .env ] && set -a && . ./.env && set +a

exec "$PY" -m app.main
