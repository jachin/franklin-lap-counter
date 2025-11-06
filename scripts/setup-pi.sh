#!/bin/bash
# Advanced Raspberry Pi setup script with idempotency and error handling

set -e

# Load .env file if it exists
# # TODO This seems silly, like shouldn't devbox just do this for us?
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Read environment variables with defaults
PI_USER="${PI_USER:-pi}"
PI_HOST="${PI_HOST:-raspberrypi.local}"
# Set PI_DEST_DIR after PI_USER is set so it uses the correct user
PI_DEST_DIR="${PI_DEST_DIR:-/home/$PI_USER/franklin-lap-counter}"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}



check_dependency() {
    local cmd="$1"
    local package="$2"
    if ssh "$PI_USER@$PI_HOST" "command -v $cmd >/dev/null 2>&1"; then
        log "✓ $cmd is already installed"
        return 0
    else
        log "⚠ $cmd not found, will install $package"
        return 1
    fi
}

check_python_module() {
    local module="$1"
    if ssh "$PI_USER@$PI_HOST" "python3 -m $module --help >/dev/null 2>&1"; then
        log "✓ Python module $module is available"
        return 0
    else
        log "⚠ Python module $module not found"
        return 1
    fi
}

ensure_service() {
    local service="$1"
    if ssh "$PI_USER@$PI_HOST" "systemctl is-active --quiet $service"; then
        log "✓ $service is running"
    else
        log "⚠ Starting $service"
        ssh "$PI_USER@$PI_HOST" "sudo systemctl enable $service && sudo systemctl start $service"
    fi
}

main() {

    log "Setting up Franklin Lap Counter on $PI_HOST"
    log "Destination directory: $PI_DEST_DIR"

    # Test SSH connectivity
    if ! ssh "$PI_USER@$PI_HOST" "echo 'SSH connection successful'" >/dev/null 2>&1; then
        log "❌ Cannot connect to $PI_HOST via SSH"
        log "   Make sure SSH is enabled and you can connect without password"
        exit 1
    fi

    log "✓ SSH connection to $PI_HOST successful"

    # Create destination directory
    ssh "$PI_USER@$PI_HOST" "mkdir -p $PI_DEST_DIR"
    log "✓ Created destination directory $PI_DEST_DIR"

    # Update package lists
    log "Updating package lists..."
    ssh "$PI_USER@$PI_HOST" "sudo apt-get update"

    # Check and install dependencies
    local packages_to_install=()

    if ! check_dependency python3 python3; then
        packages_to_install+=(python3)
    fi

    if ! check_dependency pip3 python3-pip; then
        packages_to_install+=(python3-pip)
    fi

    if ! check_dependency redis-server redis-server; then
        packages_to_install+=(redis-server)
    fi

    if ! check_dependency tmux tmux; then
        packages_to_install+=(tmux)
    fi

    if ! check_python_module venv; then
        packages_to_install+=(python3-venv)
    fi

    # Install missing packages
    if [ ${#packages_to_install[@]} -gt 0 ]; then
        log "Installing packages: ${packages_to_install[*]}"
        ssh "$PI_USER@$PI_HOST" "sudo apt-get install -y ${packages_to_install[*]}"
    else
        log "✓ All required system packages are already installed"
    fi

    # Setup Python virtual environment
    if ssh "$PI_USER@$PI_HOST" "[ -d $PI_DEST_DIR/.venv ]"; then
        log "✓ Python virtual environment exists"
    else
        log "Creating Python virtual environment..."
        ssh "$PI_USER@$PI_HOST" "cd $PI_DEST_DIR && python3 -m venv .venv"
    fi

    # Install/upgrade Python packages
    log "Installing/upgrading Python packages..."
    ssh "$PI_USER@$PI_HOST" "cd $PI_DEST_DIR && source .venv/bin/activate && pip install --upgrade pip"
    ssh "$PI_USER@$PI_HOST" "cd $PI_DEST_DIR && source .venv/bin/activate && pip install --upgrade textual redis typing-extensions aiohttp pygments rich"

    # Install tmuxinator
    log "Installing tmuxinator..."
    if ssh "$PI_USER@$PI_HOST" "command -v gem >/dev/null 2>&1"; then
        ssh "$PI_USER@$PI_HOST" "sudo gem install tmuxinator"
        log "✓ tmuxinator installed"
    else
        log "Installing Ruby and gems..."
        ssh "$PI_USER@$PI_HOST" "sudo apt-get install -y ruby-full"
        ssh "$PI_USER@$PI_HOST" "sudo gem install tmuxinator"
        log "✓ Ruby and tmuxinator installed"
    fi

    # Configure and start Redis
    ensure_service redis-server

    # Copy startup script
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    if [ -f "$SCRIPT_DIR/start-franklin.sh" ]; then
        log "Copying startup script to Pi..."
        scp "$SCRIPT_DIR/start-franklin.sh" "$PI_USER@$PI_HOST:$PI_DEST_DIR/"
        ssh "$PI_USER@$PI_HOST" "chmod +x $PI_DEST_DIR/start-franklin.sh"
        log "✓ Startup script copied"
    else
        log "⚠ Warning: start-franklin.sh not found in scripts directory"
        log "   The Pi setup is complete but you'll need to manually create a startup script"
    fi

    # Show system information
    log "System information:"
    ssh "$PI_USER@$PI_HOST" "echo '  OS: ' \$(cat /etc/os-release | grep PRETTY_NAME | cut -d'\"' -f2)"
    ssh "$PI_USER@$PI_HOST" "echo '  Python: ' \$(python3 --version)"
    ssh "$PI_USER@$PI_HOST" "echo '  glibc: ' \$(ldd --version | head -n1 | awk '{print \$NF}')"
    ssh "$PI_USER@$PI_HOST" "echo '  Redis: ' \$(redis-server --version | head -n1)"

    log "✓ Setup complete!"
    log ""
    log "Next steps:"
    log "  1. Deploy your application: devbox run deploy-pi"
    log "  2. Start the application: ssh $PI_USER@$PI_HOST 'cd $PI_DEST_DIR && ./start-franklin.sh'"
    log ""
    log "The startup script will:"
    log "  - Start Redis with Unix socket"
    log "  - Run hardware monitor in background"
    log "  - Run Franklin TUI in foreground"
    log "  - Clean shutdown with Ctrl+C"
}

main "$@"
