#!/usr/bin/env bash
# Restart Franklin GUI inside the running sway session on the Pi.

set -euo pipefail

# Load .env file if it exists (may define PI_USER / PI_HOST)
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

PI_USER="${PI_USER:-jachin}"
PI_HOST="${PI_HOST:-10.27.1.64}"
FRANKLIN_USER="${FRANKLIN_USER:-franklin}"
APP_DIR="${APP_DIR:-/home/${FRANKLIN_USER}/franklin-lap-counter}"
TMUX_SESSION_NAME="${TMUX_SESSION_NAME:-franklin-services}"

echo "Restarting Franklin GUI on ${PI_USER}@${PI_HOST} ..."

ssh -o ConnectTimeout=8 "${PI_USER}@${PI_HOST}" \
    "FRANKLIN_USER='${FRANKLIN_USER}' APP_DIR='${APP_DIR}' TMUX_SESSION_NAME='${TMUX_SESSION_NAME}' bash -s" <<'REMOTE'
set -euo pipefail

UID_F=$(id -u "$FRANKLIN_USER")
RT="/run/user/${UID_F}"

WD=$(sudo ls -1 "$RT" 2>/dev/null | grep -E '^wayland-[0-9]+$' | head -1 || true)
if [ -z "$WD" ]; then
    echo "ERROR: no Wayland socket in $RT; is sway running for $FRANKLIN_USER?" >&2
    exit 1
fi

echo "Using XDG_RUNTIME_DIR=$RT WAYLAND_DISPLAY=$WD"

echo "Stopping existing Franklin GUI/session (if running)..."
sudo pkill -u "$FRANKLIN_USER" -f "python .*franklin-gui.py" || true
sudo pkill -u "$FRANKLIN_USER" -f "start-franklin-gui-session.sh" || true
sudo -u "$FRANKLIN_USER" tmux kill-session -t "$TMUX_SESSION_NAME" 2>/dev/null || true
sleep 1

echo "Starting Franklin GUI session..."
sudo -u "$FRANKLIN_USER" XDG_RUNTIME_DIR="$RT" WAYLAND_DISPLAY="$WD" \
    setsid bash -c "cd '$APP_DIR' && nohup ./start-franklin-gui-session.sh >/dev/null 2>&1 &"
sleep 2

echo "Franklin GUI restart requested."
REMOTE

echo "Done."
