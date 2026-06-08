#!/bin/bash
# Restart wayvnc inside the running sway session on the Pi.
# wayvnc is launched by sway (not systemd), so it must be relaunched with the
# franklin user's Wayland session environment (XDG_RUNTIME_DIR + WAYLAND_DISPLAY).

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

echo "Restarting wayvnc on ${PI_USER}@${PI_HOST} ..."

ssh -o ConnectTimeout=8 "${PI_USER}@${PI_HOST}" \
    "FRANKLIN_USER='${FRANKLIN_USER}' APP_DIR='${APP_DIR}' bash -s" <<'REMOTE'
set -euo pipefail

UID_F=$(id -u "$FRANKLIN_USER")
RT="/run/user/${UID_F}"

WD=$(sudo ls -1 "$RT" 2>/dev/null | grep -E '^wayland-[0-9]+$' | head -1 || true)
if [ -z "$WD" ]; then
    echo "ERROR: no Wayland socket in $RT; is sway running for $FRANKLIN_USER?" >&2
    exit 1
fi
echo "Using XDG_RUNTIME_DIR=$RT WAYLAND_DISPLAY=$WD"

# Stop any existing wayvnc, then relaunch via sway's launcher script.
sudo pkill -u "$FRANKLIN_USER" -x wayvnc || true
sleep 1

sudo -u "$FRANKLIN_USER" XDG_RUNTIME_DIR="$RT" WAYLAND_DISPLAY="$WD" \
    setsid bash -c "cd '$APP_DIR' && nohup ./start-wayvnc.sh >/dev/null 2>&1 &"
sleep 2

echo "=== wayvnc process ==="
pgrep -af wayvnc | grep -v 'bash -lc' || echo "wayvnc NOT running"
echo "=== listening port ==="
ss -lnt | grep ':5900 ' || echo "NOT listening on 5900"
echo "=== wayvnc.log tail ==="
tail -n 10 "$APP_DIR/wayvnc.log" 2>/dev/null || true
REMOTE

echo "Done."
