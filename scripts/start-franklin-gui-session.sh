#!/usr/bin/env bash
# Start Franklin services for GUI/Wayland session

set -euo pipefail

cd "$(dirname "$0")"

HW_PID=""
WEB_PID=""

cleanup() {
  if [ -n "${WEB_PID}" ] && kill -0 "${WEB_PID}" 2>/dev/null; then
    kill "${WEB_PID}" 2>/dev/null || true
    wait "${WEB_PID}" 2>/dev/null || true
  fi

  if [ -n "${HW_PID}" ] && kill -0 "${HW_PID}" 2>/dev/null; then
    kill "${HW_PID}" 2>/dev/null || true
    wait "${HW_PID}" 2>/dev/null || true
  fi

  if [ -S ./redis.sock ]; then
    redis-cli -s ./redis.sock shutdown nosave >/dev/null 2>&1 || true
  fi

  rm -f ./redis.sock
}

trap cleanup EXIT INT TERM

if [ -d .venv ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

mkdir -p .
touch race.log hardware_redis.log redis.log web.log

echo "Starting Redis (unix socket)..."
redis-server --daemonize yes --port 0 --unixsocket ./redis.sock --unixsocketperm 700 --loglevel notice --logfile ./redis.log

echo "Starting hardware monitor..."
./franklin-hardware-monitor >> ./hardware.log 2>&1 &
HW_PID=$!

echo "Starting web server..."
python web_server.py >> ./web.log 2>&1 &
WEB_PID=$!

echo "Starting Franklin GTK GUI..."
exec python franklin-gui.py --race
