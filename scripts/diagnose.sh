#!/usr/bin/env bash
# Franklin Lap Counter — on-Pi diagnostic script
#
# Run directly on the Raspberry Pi (no Ansible needed):
#   sudo ./scripts/diagnose.sh
#
# Checks services, logs, filesystem, and web app connectivity.
###############################################################################

set -euo pipefail

APP_DIR="/opt/franklin-lap-counter"
LOG_DIR="/var/log/franklin"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

pass() { echo -e "  ${GREEN}✓${NC} $*"; }
fail() { echo -e "  ${RED}✗${NC} $*"; }
warn() { echo -e "  ${YELLOW}⚠${NC} $*"; }
info() { echo -e "${BLUE}[INFO]${NC}  $*"; }
header() { echo -e "\n${BLUE}── $* ──${NC}"; }

SERVICES=(
    franklin-redis
    franklin-hardware-monitor
    franklin-race-recorder
    franklin-web-scoreboard
    franklin-web-referee
    franklin-web-healthcheck
    franklin-web-driver
)

check_system() {
    header "System"
    echo "  Host:  $(hostname)"
    echo "  Uptime:$(uptime -p 2>/dev/null || uptime)"
    echo "  Memory:$(free -h | awk '/Mem:/ {print $3 "/" $2}')"
    echo "  Disk:  $(df -h "$APP_DIR" 2>/dev/null | awk 'NR==2{print $3 "/" $2 " (" $5 ")"}')"
}

check_target() {
    header "franklin.target"
    local state
    state=$(systemctl is-active franklin.target 2>/dev/null || echo "unknown")
    if [[ "$state" == "active" ]]; then
        pass "franklin.target is active"
    else
        fail "franklin.target is $state"
    fi
}

check_services() {
    header "Individual services"
    for svc in "${SERVICES[@]}"; do
        local state
        state=$(systemctl is-active "$svc" 2>/dev/null || echo "unknown")
        if [[ "$state" == "active" ]]; then
            pass "$svc"
        else
            fail "$svc ($state)"
        fi
    done
}

check_logs_for_failures() {
    header "Recent logs (failed/inactive services only)"
    local any=false
    for svc in "${SERVICES[@]}"; do
        local state
        state=$(systemctl is-active "$svc" 2>/dev/null || echo "unknown")
        if [[ "$state" != "active" ]]; then
            any=true
            echo -e "\n  ── $svc ──"
            journalctl -u "$svc" --no-pager -n 15 2>&1 | sed 's/^/    /'
        fi
    done
    $any || echo "  (all services active, no failure logs to show)"
}

check_app_dir() {
    header "Application directory: $APP_DIR"
    if [[ ! -d "$APP_DIR" ]]; then
        fail "Directory does not exist!"
        return
    fi
    pass "Directory exists"

    local missing=0
    for f in \
        .venv/bin/python \
        franklin-tui.py \
        franklin-gui.py \
        franklin-race-recorder.py \
        driver_web_app.py \
        referee_web_app.py \
        scoreboard_web_app.py \
        healthcheck_web_app.py \
        redis_commands.py \
        database.py \
        race \
        static \
        start_franklin.py \
        start_franklin_gui_session.py; do
        if [[ -e "$APP_DIR/$f" ]]; then
            pass "$f"
        else
            fail "$f (MISSING)"
            ((missing++))
        fi
    done
    echo
    if [[ $missing -eq 0 ]]; then
        pass "All critical files present"
    else
        fail "$missing file(s) missing"
    fi
}

check_redis() {
    header "Redis socket"
    if [[ -S "$APP_DIR/run/redis.sock" ]]; then
        pass "redis.sock exists"
        local ping
        ping=$(redis-cli -s "$APP_DIR/run/redis.sock" PING 2>/dev/null || true)
        if [[ "$ping" == "PONG" ]]; then
            pass "redis PONG"
        else
            fail "redis not responding: $ping"
        fi
    else
        fail "redis.sock not found at $APP_DIR/run/redis.sock"
    fi
}

check_web_apps() {
    header "Web app connectivity"
    local apps=(
        "scoreboard:8085"
        "referee:8081"
        "healthcheck:8082"
        "driver:8083"
    )
    for app in "${apps[@]}"; do
        local name="${app%%:*}"
        local port="${app##*:}"
        local code
        code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "http://127.0.0.1:$port" 2>/dev/null || true)
        if [[ -n "$code" ]]; then
            pass "$name (port $port)  HTTP $code"
        else
            fail "$name (port $port)  unreachable"
        fi
    done
}

check_log_files() {
    header "Log files ($LOG_DIR)"
    if [[ ! -d "$LOG_DIR" ]]; then
        fail "Log directory does not exist"
        return
    fi
    local count
    count=$(ls -1 "$LOG_DIR"/*.log 2>/dev/null | wc -l)
    if [[ $count -eq 0 ]]; then
        warn "No .log files found in $LOG_DIR"
    else
        pass "$count log file(s)"
        ls -lht "$LOG_DIR"/*.log 2>/dev/null | sed 's/^/  /'
    fi
}

check_binary() {
    header "Hardware monitor binary"
    local paths=(
        "/usr/bin/franklin-hardware-monitor"
        "$APP_DIR/bin/franklin-hardware-monitor"
    )
    local found=false
    for p in "${paths[@]}"; do
        if [[ -x "$p" ]]; then
            pass "$p"
            file "$p" 2>/dev/null | sed 's/^/    /'
            found=true
        fi
    done
    $found || fail "franklin-hardware-monitor not found (try: which franklin-hardware-monitor)"
}

# ── main ────────────────────────────────────────────────────────────────────

if [[ $EUID -ne 0 ]]; then
    echo "This script should be run as root (sudo) for full log access."
    echo "Continuing without root — some checks may be incomplete."
    echo
fi

echo ""
echo "═══════════════════════════════════════════════"
echo "  Franklin Lap Counter — On-Pi Diagnostics"
echo "  $(date)"
echo "═══════════════════════════════════════════════"

check_system
check_target
check_services
check_logs_for_failures
check_app_dir
check_redis
check_web_apps
check_log_files
check_binary

echo ""
echo "═══════════════════════════════════════════════"
echo "  Diagnostics complete."
echo "  To watch live logs: journalctl -u franklin-redis -f"
echo "═══════════════════════════════════════════════"
echo ""
