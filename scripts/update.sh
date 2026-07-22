#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# Franklin Lap Counter — self-update
#
# Lightweight update script intended to be run on the Pi. Pulls the latest
# code, rebuilds the Rust binary if needed, and restarts changed services.
#
# Usage:
#   sudo ./update.sh                          # online — git pull + rebuild
#   sudo ./update.sh --offline                # offline — uses local files only
###############################################################################

APP_DIR="/opt/franklin-lap-counter"
BIN_DIR="${APP_DIR}/bin"
LOG_DIR="/var/log/franklin"
STATIC_DIR="${APP_DIR}/static"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

OFFLINE=false
for arg in "$@"; do
  case "$arg" in
    --offline) OFFLINE=true ;;
    --help)    echo "Usage: sudo $0 [--offline]"; exit 0 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root (sudo)."
    exit 1
fi

if [[ ! -d "$APP_DIR" ]]; then
    error "Franklin not installed at $APP_DIR. Run install.sh first."
    exit 1
fi

info "Franklin Lap Counter update starting...${OFFLINE:+ (offline mode)}"

# ── update code ──────────────────────────────────────────────────────────

UPDATED=false

if $OFFLINE; then
    info "Offline mode — using local files as-is"
elif [[ -d "$APP_DIR/.git" ]]; then
    info "Pulling latest code from git..."
    OLD_COMMIT=$(git -C "$APP_DIR" rev-parse HEAD 2>/dev/null || true)
    if git -C "$APP_DIR" pull --ff-only 2>/dev/null; then
        NEW_COMMIT=$(git -C "$APP_DIR" rev-parse HEAD 2>/dev/null || true)
        if [[ "$OLD_COMMIT" != "$NEW_COMMIT" ]]; then
            UPDATED=true
            info "Code updated ($(echo "$OLD_COMMIT" | head -c 7)... \u2192 $(echo "$NEW_COMMIT" | head -c 7)...)"
        else
            info "Already up to date."
        fi
    else
        warn "git pull failed, continuing with existing code"
    fi
else
    warn "Not a git repository; update via git not available."
fi

# ── re-deploy lib files ──────────────────────────────────────────────────

STATIC_DIR="${APP_DIR}/static"

info "Syncing application files..."
copy_python() {
    local file="$1"
    if [[ -f "$APP_DIR/$file" ]]; then
        cp "$APP_DIR/$file" "$APP_DIR/"
    fi
}

for f in franklin-race-recorder.py franklin-gui.py franklin-tui.py \
         scoreboard_web_app.py referee_web_app.py healthcheck_web_app.py \
         driver_web_app.py database.py gui_config.py racer_colors.py \
         redis_commands.py start_franklin_gui_session.py; do
    copy_python "$f"
done

if [[ -d "$APP_DIR/race" ]]; then
    rm -rf "$APP_DIR/race"
    cp -r "$APP_DIR/race" "$APP_DIR/race"
fi

if [[ -d "$APP_DIR/static" ]]; then
    rm -rf "$STATIC_DIR"
    cp -r "$APP_DIR/static" "$STATIC_DIR"
fi

cp "$APP_DIR/scripts/start_franklin.py" "$BIN_DIR/franklin-start"

# ── update Python deps ──────────────────────────────────────────────────

VENV_DIR="${APP_DIR}/.venv"
if [[ -d "$VENV_DIR" ]]; then
    info "Upgrading Python dependencies..."
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    if [[ -f "$APP_DIR/pyproject.toml" ]]; then
        "$VENV_DIR/bin/pip" install --quiet -e "$APP_DIR" 2>/dev/null || true
    fi
fi

# ── deploy Rust binary ──────────────────────────────────────────────────

if [[ -f "$APP_DIR/rust/target/release/franklin-hardware-monitor" ]]; then
    info "Deploying Rust binary from source build..."
    cp "$APP_DIR/rust/target/release/franklin-hardware-monitor" "$BIN_DIR/"
elif [[ -f "$BIN_DIR/franklin-hardware-monitor" ]]; then
    info "Rust binary already present at $BIN_DIR"
elif $OFFLINE; then
    warn "No Rust binary found at $BIN_DIR — hardware monitor will not be available"
else
    warn "No Rust binary found. Build it with: cargo build --release --manifest-path rust/Cargo.toml"
fi

# ── update systemd service files ─────────────────────────────────────────

info "Syncing systemd service files..."
SYSTEMD_SRC="${APP_DIR}/systemd"
if [[ -d "$SYSTEMD_SRC" ]]; then
    for unit in "$SYSTEMD_SRC"/*.service "$SYSTEMD_SRC"/*.target; do
        if [[ -f "$unit" ]]; then
            cp "$unit" /etc/systemd/system/
        fi
    done
    systemctl daemon-reload
fi

# ── restart services ─────────────────────────────────────────────────────

info "Restarting Franklin services..."
systemctl restart franklin.target 2>/dev/null || {
    warn "Failed to restart franklin.target, starting individual services..."
    systemctl start franklin.target 2>/dev/null || true
}

# ── summary ──────────────────────────────────────────────────────────────

echo ""
info "Update complete!"
echo ""
echo "  Active services:"
systemctl list-units --no-pager "franklin-*" --all 2>/dev/null | grep -v "^  " | grep -v "^$" || true
echo ""
echo "  To check status:  systemctl status franklin.target"
echo "  To view logs:     journalctl -u franklin-redis -f"
