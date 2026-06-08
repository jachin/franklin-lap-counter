#!/usr/bin/env bash
# Start Franklin GUI with tmux-managed backend services

set -euo pipefail

cd "$(dirname "$0")"

TMUX_SESSION_NAME="${TMUX_SESSION_NAME:-franklin-services}"
TMUXINATOR_CONFIG="${TMUXINATOR_CONFIG:-tmuxinator/franklin-services.yml}"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

cleanup() {
  if tmux has-session -t "${TMUX_SESSION_NAME}" 2>/dev/null; then
    log "Stopping tmux session: ${TMUX_SESSION_NAME}"
    tmux kill-session -t "${TMUX_SESSION_NAME}" || true
  fi

  rm -f ./redis.sock
}

trap cleanup EXIT INT TERM

if [ -d .venv ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

touch gui.log hardware_redis.log redis.log web.log

if ! command -v tmux >/dev/null 2>&1; then
  log "❌ tmux not found"
  exit 1
fi

if ! command -v tmuxinator >/dev/null 2>&1; then
  log "❌ tmuxinator not found"
  exit 1
fi

if [ ! -f "${TMUXINATOR_CONFIG}" ]; then
  log "❌ Missing tmuxinator config: ${TMUXINATOR_CONFIG}"
  exit 1
fi

if tmux has-session -t "${TMUX_SESSION_NAME}" 2>/dev/null; then
  log "Tmux session '${TMUX_SESSION_NAME}' already running; reusing it"
else
  log "Starting tmux services via ${TMUXINATOR_CONFIG}"
  tmuxinator start -p "${TMUXINATOR_CONFIG}" --no-attach
fi

log "Starting Franklin GTK GUI (using saved mode preference unless CLI override is provided)..."
exec python franklin-gui.py >> ./gui.log 2>&1
