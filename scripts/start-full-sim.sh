#!/usr/bin/env bash
set -euo pipefail

HW_PID=""
WEB_PID=""

cleanup() {
  if [ -n "${WEB_PID}" ] && kill -0 "${WEB_PID}" 2>/dev/null; then
    echo ""
    echo "Stopping web server (pid ${WEB_PID})..."
    kill "${WEB_PID}" 2>/dev/null || true
    wait "${WEB_PID}" 2>/dev/null || true
  fi

  if [ -n "${HW_PID}" ] && kill -0 "${HW_PID}" 2>/dev/null; then
    echo ""
    echo "Stopping hardware simulator (pid ${HW_PID})..."
    kill "${HW_PID}" 2>/dev/null || true
    wait "${HW_PID}" 2>/dev/null || true
  fi
}

trap cleanup EXIT INT TERM

echo "Building Rust project..."
cargo build --manifest-path rust/Cargo.toml

echo "Starting hardware simulator..."
cargo run --manifest-path rust/Cargo.toml --bin franklin-hardware-monitor -- --sim &
HW_PID=$!

echo "Starting web server..."
python web_server.py &
WEB_PID=$!

echo "Starting Franklin in sim mode (race UI)..."
python franklin.py --race
