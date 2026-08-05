#!/usr/bin/env bash
# Pull changes, reinstall anything new, and restart Dowser.
#
#   deploy/update.sh           # warns if downloads are in flight
#   deploy/update.sh --force   # restart anyway
#
# Restarting mid-download is safe: in-flight jobs are re-queued and resume on
# the way back up. They restart from the beginning, though, so a nearly finished
# large download is worth waiting for.
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

PORT="${PORT:-8477}"
[ -f .env ] && PORT="$(grep -E '^\s*PORT=' .env | tail -1 | cut -d= -f2 | tr -d ' "' || echo "$PORT")"

# --- warn about work in progress --------------------------------------------

ACTIVE="$(curl -fsS --max-time 3 "http://127.0.0.1:$PORT/api/health" 2>/dev/null \
  | sed -n 's/.*"active":\([0-9]*\).*/\1/p' || true)"

if [ -n "$ACTIVE" ] && [ "$ACTIVE" -gt 0 ] && [ "$FORCE" -eq 0 ]; then
  echo "$ACTIVE download(s) are running right now."
  echo "They will resume after the restart, but from the beginning."
  read -rp "Restart anyway? [y/N] " answer
  case "$answer" in [yY]*) ;; *) echo "Left alone."; exit 0 ;; esac
fi

# --- code --------------------------------------------------------------------

REQ_BEFORE=""
[ -f requirements.txt ] && REQ_BEFORE="$(shasum requirements.txt | cut -d' ' -f1)"

if [ -d .git ]; then
  echo "==> Pulling changes"
  git pull --ff-only
else
  echo "==> Not a git checkout; assuming files are already updated"
fi

# --- dependencies ------------------------------------------------------------

PY=".venv/bin/python"
if [ -x "$PY" ] && [ -n "$REQ_BEFORE" ]; then
  if [ "$REQ_BEFORE" != "$(shasum requirements.txt | cut -d' ' -f1)" ]; then
    echo "==> requirements.txt changed; reinstalling"
    "$PY" -m pip install --quiet -r requirements.txt
    "$PY" -m playwright install chromium >/dev/null 2>&1 || true
  fi
fi

# --- restart -----------------------------------------------------------------

if systemctl list-unit-files 2>/dev/null | grep -q '^dowser\.service'; then
  echo "==> Restarting the dowser service"
  ${SUDO:-sudo} systemctl restart dowser
  sleep 2
  systemctl is-active --quiet dowser \
    && echo "==> Running. Logs: journalctl -u dowser -f" \
    || { echo "==> Failed to start:" >&2; journalctl -u dowser -n 20 --no-pager >&2; exit 1; }
else
  echo "==> No systemd unit found (install it with: sudo deploy/install-service.sh)"
  echo "    Restart however you started it, e.g. ./run.sh"
fi
