#!/bin/bash
# Install tmuxinator and dependencies on Raspberry Pi

set -e

# Load .env file if it exists
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Configuration
PI_USER="${PI_USER:-pi}"
PI_HOST="${PI_HOST:-raspberrypi.local}"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

check_dependency() {
    local cmd="$1"
    if ssh "$PI_USER@$PI_HOST" "command -v $cmd >/dev/null 2>&1"; then
        log "✓ $cmd is available"
        return 0
    else
        log "⚠ $cmd not found"
        return 1
    fi
}

install_tmuxinator() {
    log "Installing tmuxinator dependencies on Raspberry Pi..."

    # Test SSH connectivity
    if ! ssh "$PI_USER@$PI_HOST" "echo 'SSH connection test'" >/dev/null 2>&1; then
        log "❌ Cannot connect to $PI_HOST via SSH"
        exit 1
    fi

    log "✓ SSH connection successful"

    # Update package lists
    log "Updating package lists..."
    ssh "$PI_USER@$PI_HOST" "sudo apt-get update"

    # Install tmux if needed
    if ! check_dependency tmux; then
        log "Installing tmux..."
        ssh "$PI_USER@$PI_HOST" "sudo apt-get install -y tmux"
    fi

    # Install Ruby if needed (for tmuxinator gem)
    if ! check_dependency ruby; then
        log "Installing Ruby..."
        ssh "$PI_USER@$PI_HOST" "sudo apt-get install -y ruby-full build-essential"
    fi

    # Install tmuxinator gem
    if ! check_dependency tmuxinator; then
        log "Installing tmuxinator gem..."
        ssh "$PI_USER@$PI_HOST" "sudo gem install tmuxinator"
    fi

    # Verify installation
    log "Verifying tmuxinator installation..."
    TMUX_VERSION=$(ssh "$PI_USER@$PI_HOST" "tmux -V" || echo "unknown")
    TMUXINATOR_VERSION=$(ssh "$PI_USER@$PI_HOST" "tmuxinator version" || echo "unknown")

    log "✓ Installation complete!"
    log "  tmux: $TMUX_VERSION"
    log "  tmuxinator: $TMUXINATOR_VERSION"
    log ""
    log "You can now use tmuxinator to manage Franklin sessions."
}

main() {
    install_tmuxinator
}

main "$@"
