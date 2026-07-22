#!/usr/bin/env bash
set -euo pipefail

# Start all Franklin development services (simulator mode, no tmux/systemd).
# Redis is expected to be running already (devbox init_hook starts it).
#
# Usage: ./scripts/start_dev.sh [--with-tui]
#   --with-tui  Also launch the TUI (interactive, takes over terminal)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$ROOT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

WITH_TUI=false
for arg in "$@"; do
    case "$arg" in
        --with-tui) WITH_TUI=true ;;
    esac
done

cleanup() {
    info "Shutting down all services..."
    pkill -f 'franklin-hardware-monitor.*--sim' 2>/dev/null || true
    pkill -f 'python franklin-race-recorder' 2>/dev/null || true
    pkill -f 'python .*_web_app.py' 2>/dev/null || true
    pkill -f 'python franklin-tui.py' 2>/dev/null || true
    info "All services stopped"
}
trap cleanup EXIT INT TERM

# Start hardware monitor (simulator mode)
info "Starting hardware monitor (simulator)..."
cargo run --manifest-path rust/Cargo.toml --bin franklin-hardware-monitor -- --sim \
    >>hardware_monitor.log 2>&1 &
HW_PID=$!
sleep 2

# Start race recorder (shadow mode for dev)
info "Starting race recorder (shadow)..."
python franklin-race-recorder.py --shadow \
    >>race_recorder.log 2>&1 &
RECORDER_PID=$!
sleep 1

# Start web apps (both stdout/stderr redirected to log files so output
# doesn't corrupt the TUI terminal when run alongside it).
info "Starting web apps..."
python scoreboard_web_app.py >>web_scoreboard.log 2>&1 &
python referee_web_app.py >>web_referee.log 2>&1 &
python healthcheck_web_app.py >>web_healthcheck.log 2>&1 &
python driver_web_app.py >>web_driver.log 2>&1 &
sleep 1

info "All background services running"

if $WITH_TUI; then
    info "Launching TUI..."
    python franklin-tui.py --fake
else
    info "Services running in background. Press Ctrl+C to stop all."
    info ""
    info "  Web apps:"
    info "    Scoreboard:   http://localhost:8085"
    info "    Referee:      http://localhost:8081"
    info "    Health check: http://localhost:8082"
    info "    Driver:       http://localhost:8083"
    info ""
    info "  Run TUI separately: python franklin-tui.py --fake"
    wait
fi
