#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# Franklin Lap Counter — deploy to Pi over LAN (offline-capable)
#
# Builds the Rust binary, rsyncs the entire project to the Pi as the franklin
# user, then SSHs in and runs update.sh --offline.
#
# Usage:
#   export FRANKLIN_PI_HOST=raspberrypi.local
#   bash scripts/deploy_pi.sh
#
#   # Or with --host flag:
#   bash scripts/deploy_pi.sh --host raspberrypi.local
#
# Configuration via .env (project root):
#   FRANKLIN_PI_HOST=raspberrypi.local
#
# Requires:
#   - SSH key-based auth to franklin@<host> (no password prompt)
#   - franklin user has passwordless sudo for update.sh
###############################################################################

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

# ── load .env ────────────────────────────────────────────────────────────

if [[ -f ".env" ]]; then
    set -a
    source .env
    set +a
fi

# ── parse arguments ──────────────────────────────────────────────────────

PI_HOST="${FRANKLIN_PI_HOST:-}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) PI_HOST="$2"; shift 2 ;;
        --help)
            echo "Usage: $0 [--host <hostname>]"
            echo ""
            echo "Deploys Franklin to a Pi over the local network."
            echo "Set FRANKLIN_PI_HOST in .env or use --host flag."
            exit 0
            ;;
        *) error "Unknown option: $1"; exit 1 ;;
    esac
done

if [[ -z "$PI_HOST" ]]; then
    error "No Pi host specified."
    error "Set FRANKLIN_PI_HOST in .env or pass --host <hostname>"
    echo ""
    echo "  Example:"
    echo "    echo 'FRANKLIN_PI_HOST=raspberrypi.local' >> .env"
    echo "    bash scripts/deploy_pi.sh"
    exit 1
fi

PI_USER="franklin"
PI_DIR="/opt/franklin-lap-counter"

# ── build Rust binary ────────────────────────────────────────────────────

info "Building Rust binary for Pi (aarch64)..."
if ! cargo build --release --manifest-path rust/Cargo.toml --target aarch64-unknown-linux-gnu 2>&1 | tail -3; then
    warn "Cross-build failed. Will only rsync source (Rust build will be skipped on Pi in offline mode)."
fi

# ── rsync to Pi ──────────────────────────────────────────────────────────

EXCLUDES=(
    ".venv"
    "__pycache__"
    "*.pyc"
    "rust/target"
    ".git"
    "redis.sock"
    "*.log"
    "*.sock"
    ".env"
    "devbox.json"
    "devbox.lock"
    "devbox.d"
    ".github"
)

RSYNC_ARGS="-avz --delete"
for exc in "${EXCLUDES[@]}"; do
    RSYNC_ARGS+=" --exclude=$exc"
done

info "Syncing files to ${PI_USER}@${PI_HOST}:${PI_DIR}/..."
if ! rsync $RSYNC_ARGS ./ "${PI_USER}@${PI_HOST}:${PI_DIR}/"; then
    error "rsync failed. Is ${PI_USER}@${PI_HOST} reachable?"
    exit 1
fi

# ── run offline update on Pi ─────────────────────────────────────────────

info "Running offline update on Pi..."
if ! ssh "${PI_USER}@${PI_HOST}" "sudo ${PI_DIR}/scripts/update.sh --offline"; then
    warn "update.sh exited with non-zero status (may be partial failure)."
    warn "SSH in to diagnose: ssh ${PI_USER}@${PI_HOST}"
fi

# ── done ─────────────────────────────────────────────────────────────────

echo ""
info "Deploy complete!"
echo "  Pi host:  ${PI_HOST}"
echo "  App dir:  ${PI_DIR}"
echo "  User:     ${PI_USER}"
echo ""
echo "  SSH in:   ssh ${PI_USER}@${PI_HOST}"
echo "  Status:   ssh ${PI_USER}@${PI_HOST} 'systemctl status franklin.target'"
echo "  Logs:     ssh ${PI_USER}@${PI_HOST} 'journalctl -u franklin-redis -f'"
