#!/bin/bash
# Deploy Franklin Lap Counter to Raspberry Pi

set -e

# Load .env file if it exists
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Configuration with defaults
PI_USER="${PI_USER:-pi}"
PI_HOST="${PI_HOST:-raspberrypi.local}"
PI_DEST_DIR="${PI_DEST_DIR:-/home/$PI_USER/franklin-lap-counter}"
PI_ARCH="${RUST_PI_TARGET:-aarch64-unknown-linux-gnu}"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

# Common rsync exclusions for Python projects
RSYNC_EXCLUDES="--exclude __pycache__ --exclude *.pyc --exclude .DS_Store --exclude .pytest_cache --exclude .ruff_cache --exclude *.pyc --exclude .git --exclude .devbox --exclude .venv --exclude target"

check_file() {
    local file="$1"
    local description="$2"
    if [ -f "$file" ]; then
        log "✓ Found $description: $file"
        return 0
    else
        log "❌ Missing $description: $file"
        return 1
    fi
}

main() {
    log "Deploying Franklin Lap Counter to Raspberry Pi"
    log "Target: $PI_USER@$PI_HOST:$PI_DEST_DIR"
    log "Architecture: $PI_ARCH"
    log ""

    # Get script and project directories
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
    cd "$PROJECT_DIR"

    # Check prerequisites
    log "Checking prerequisites..."

    # Check for Rust binary
    RUST_BINARY="rust/target/$PI_ARCH/release/franklin-hardware-monitor"
    if ! check_file "$RUST_BINARY" "Rust hardware monitor binary"; then
        log "   Build it first with: devbox run rust-pi-build"
        exit 1
    fi

    # Check for Python files
    if ! check_file "franklin.py" "Franklin TUI script"; then
        exit 1
    fi

    if ! check_file "web_server.py" "Web server script"; then
        exit 1
    fi

    # Check for startup script
    if ! check_file "scripts/start-franklin.sh" "Startup script"; then
        exit 1
    fi

    # Test SSH connectivity
    log "Testing SSH connection..."
    if ! ssh "$PI_USER@$PI_HOST" "echo 'SSH connection successful'" >/dev/null 2>&1; then
        log "❌ Cannot connect to $PI_HOST via SSH"
        log "   Make sure SSH is enabled and you can connect without password"
        exit 1
    fi
    log "✓ SSH connection successful"

    # Create destination directory
    log "Creating destination directory..."
    ssh "$PI_USER@$PI_HOST" "mkdir -p $PI_DEST_DIR"

    # Copy Rust binary
    log "Deploying Rust binary..."
    scp "$RUST_BINARY" "$PI_USER@$PI_HOST:$PI_DEST_DIR/"
    ssh "$PI_USER@$PI_HOST" "chmod +x $PI_DEST_DIR/franklin-hardware-monitor"
    log "✓ Hardware monitor binary deployed"

    # Copy Python files and dependencies
    log "Deploying Python application files..."

    # Main application files
    scp "franklin.py" "$PI_USER@$PI_HOST:$PI_DEST_DIR/"
    scp "web_server.py" "$PI_USER@$PI_HOST:$PI_DEST_DIR/"
    scp "database.py" "$PI_USER@$PI_HOST:$PI_DEST_DIR/" 2>/dev/null || log "  (database.py not found, skipping)"

    # Copy configuration files
    log "Deploying configuration files..."
    scp "franklin.config.json" "$PI_USER@$PI_HOST:$PI_DEST_DIR/" 2>/dev/null || log "  (franklin.config.json not found, skipping)"
    scp "pyrightconfig.json" "$PI_USER@$PI_HOST:$PI_DEST_DIR/" 2>/dev/null || log "  (pyrightconfig.json not found, skipping)"

    # Copy static directory if it exists
    if [ -d "static" ]; then
        log "Deploying static files..."
        rsync -av $RSYNC_EXCLUDES "static/" "$PI_USER@$PI_HOST:$PI_DEST_DIR/static/"
    else
        log "  (static directory not found, skipping)"
    fi

    # Copy race directory if it exists
    if [ -d "race" ]; then
        log "Deploying race files..."
        rsync -av $RSYNC_EXCLUDES "race/" "$PI_USER@$PI_HOST:$PI_DEST_DIR/race/"
    else
        log "  (race directory not found, skipping)"
    fi

    # Copy tmuxinator configuration
    if [ -d "tmuxinator" ]; then
        log "Deploying tmuxinator configuration..."
        rsync -av $RSYNC_EXCLUDES "tmuxinator/" "$PI_USER@$PI_HOST:$PI_DEST_DIR/tmuxinator/"
    else
        log "  (tmuxinator directory not found, skipping)"
    fi

    # Copy startup script
    log "Deploying startup script..."
    scp "scripts/start-franklin.sh" "$PI_USER@$PI_HOST:$PI_DEST_DIR/"
    ssh "$PI_USER@$PI_HOST" "chmod +x $PI_DEST_DIR/start-franklin.sh"

    # Copy .env file if it exists (for Pi-specific configuration)
    if [ -f ".env" ]; then
        log "Copying environment configuration..."
        scp ".env" "$PI_USER@$PI_HOST:$PI_DEST_DIR/"
    fi

    # Verify deployment
    log "Verifying deployment..."

    # Check if files exist on Pi
    ssh "$PI_USER@$PI_HOST" "cd $PI_DEST_DIR && ls -la franklin-hardware-monitor franklin.py web_server.py start-franklin.sh" >/dev/null
    log "✓ All main files deployed successfully"

    # Check binary architecture
    BINARY_ARCH=$(ssh "$PI_USER@$PI_HOST" "cd $PI_DEST_DIR && file franklin-hardware-monitor" | grep -o "ARM aarch64\|ARM 64-bit" || true)
    if [ -n "$BINARY_ARCH" ]; then
        log "✓ Binary architecture verified: $BINARY_ARCH"
    else
        log "⚠ Warning: Could not verify binary architecture"
    fi

    # Show deployment summary
    log ""
    log "✓ Deployment complete!"
    log ""
    log "Deployed components:"
    log "  • franklin-hardware-monitor (Rust binary)"
    log "  • franklin.py (TUI application)"
    log "  • web_server.py (Web interface)"
    log "  • start-franklin.sh (Startup script with tmuxinator)"
    log "  • tmuxinator configuration"
    log "  • Configuration files"
    log "  • Static assets and race data"
    log ""
    log "Next steps:"
    log "  1. SSH to your Pi: ssh $PI_USER@$PI_HOST"
    log "  2. Navigate to app: cd $PI_DEST_DIR"
    log "  3. Start Franklin: ./start-franklin.sh"
    log ""
    log "Or start directly with:"
    log "  ssh $PI_USER@$PI_HOST 'cd $PI_DEST_DIR && ./start-franklin.sh'"
}

main "$@"
