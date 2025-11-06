#!/bin/bash
# Franklin Lap Counter startup script using tmuxinator

set -e

# Load .env file if it exists
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Navigate to the script's directory (should be the project root on Pi)
cd "$(dirname "$0")"

# Configuration
PI_DEST_DIR="${PI_DEST_DIR:-$(pwd)}"
TMUX_SESSION_NAME="${TMUX_SESSION_NAME:-franklin}"
TMUXINATOR_CONFIG="tmuxinator/franklin.yml"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

check_dependencies() {
    local missing_deps=()

    # Check for tmux
    if ! command -v tmux &> /dev/null; then
        missing_deps+=("tmux")
    fi

    # Check for tmuxinator
    if ! command -v tmuxinator &> /dev/null; then
        missing_deps+=("tmuxinator")
    fi

    if [ ${#missing_deps[@]} -gt 0 ]; then
        log "❌ Missing dependencies: ${missing_deps[*]}"
        log "   Please install them first:"
        log "   sudo apt-get install tmux"
        log "   sudo gem install tmuxinator"
        return 1
    fi

    return 0
}

check_files() {
    local missing_files=()

    # Check for tmuxinator config
    if [ ! -f "$TMUXINATOR_CONFIG" ]; then
        missing_files+=("$TMUXINATOR_CONFIG")
    fi

    # Check for essential files
    if [ ! -f "franklin-hardware-monitor" ]; then
        missing_files+=("franklin-hardware-monitor")
    fi

    if [ ! -f "franklin.py" ]; then
        missing_files+=("franklin.py")
    fi

    if [ ! -f "web_server.py" ]; then
        missing_files+=("web_server.py")
    fi

    if [ ! -d ".venv" ]; then
        missing_files+=(".venv (Python virtual environment)")
    fi

    if [ ${#missing_files[@]} -gt 0 ]; then
        log "❌ Missing files: ${missing_files[*]}"
        log "   Run deployment first: devbox run deploy-pi"
        return 1
    fi

    return 0
}

stop_franklin() {
    log "Stopping Franklin Lap Counter..."

    # Check if tmux session exists
    if tmux has-session -t "$TMUX_SESSION_NAME" 2>/dev/null; then
        log "Killing tmux session: $TMUX_SESSION_NAME"
        tmux kill-session -t "$TMUX_SESSION_NAME"
        log "✓ Session stopped"
    else
        log "No running session found"
    fi

    # Clean up any remaining processes
    pkill -f "franklin-hardware-monitor" 2>/dev/null || true
    pkill -f "redis-server.*redis.sock" 2>/dev/null || true

    # Clean up socket file
    rm -f redis.sock

    log "✓ Franklin Lap Counter stopped"
}

start_franklin() {
    log "Starting Franklin Lap Counter with tmuxinator..."

    # Export environment variables for tmuxinator
    export PI_DEST_DIR="$PI_DEST_DIR"
    export TMUX_SESSION_NAME="$TMUX_SESSION_NAME"

    # Check if session already exists
    if tmux has-session -t "$TMUX_SESSION_NAME" 2>/dev/null; then
        log "⚠ Session '$TMUX_SESSION_NAME' already exists"
        read -p "Kill existing session and restart? (y/N): " -r
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            stop_franklin
        else
            log "Attaching to existing session..."
            tmux attach-session -t "$TMUX_SESSION_NAME"
            return
        fi
    fi

    # Start with tmuxinator
    log "Starting tmux session with configuration: $TMUXINATOR_CONFIG"
    tmuxinator start -p "$TMUXINATOR_CONFIG"
}

status_franklin() {
    log "Franklin Lap Counter Status:"

    # Check tmux session
    if tmux has-session -t "$TMUX_SESSION_NAME" 2>/dev/null; then
        log "✓ Tmux session '$TMUX_SESSION_NAME' is running"

        # Show windows
        log "Active windows:"
        tmux list-windows -t "$TMUX_SESSION_NAME" | sed 's/^/  /'
    else
        log "❌ No tmux session found"
    fi

    # Check individual processes
    log ""
    log "Process status:"

    if pgrep -f "redis-server.*redis.sock" >/dev/null 2>&1; then
        log "  ✓ Redis server running"
    else
        log "  ❌ Redis server not running"
    fi

    if pgrep -f "franklin-hardware-monitor" >/dev/null 2>&1; then
        log "  ✓ Hardware monitor running"
    else
        log "  ❌ Hardware monitor not running"
    fi

    if pgrep -f "franklin.py" >/dev/null 2>&1; then
        log "  ✓ Franklin TUI running"
    else
        log "  ❌ Franklin TUI not running"
    fi

    if pgrep -f "web_server.py" >/dev/null 2>&1; then
        log "  ✓ Web server running"
    else
        log "  ❌ Web server not running"
    fi
}

attach_franklin() {
    if tmux has-session -t "$TMUX_SESSION_NAME" 2>/dev/null; then
        log "Attaching to Franklin session..."
        tmux attach-session -t "$TMUX_SESSION_NAME"
    else
        log "❌ No Franklin session found. Start it first."
        exit 1
    fi
}

usage() {
    echo "Usage: $0 [start|stop|status|attach|restart]"
    echo ""
    echo "Commands:"
    echo "  start   - Start Franklin Lap Counter with tmuxinator"
    echo "  stop    - Stop all Franklin processes and tmux session"
    echo "  status  - Show status of Franklin components"
    echo "  attach  - Attach to existing tmux session"
    echo "  restart - Stop and start Franklin"
    echo ""
    echo "If no command is provided, 'start' is assumed."
}

main() {
    local command="${1:-start}"

    case "$command" in
        start)
            log "Franklin Lap Counter Startup"

            # Check dependencies and files
            if ! check_dependencies; then
                exit 1
            fi

            if ! check_files; then
                exit 1
            fi

            start_franklin
            ;;
        stop)
            stop_franklin
            ;;
        status)
            status_franklin
            ;;
        attach)
            attach_franklin
            ;;
        restart)
            stop_franklin
            sleep 2
            if check_dependencies && check_files; then
                start_franklin
            else
                exit 1
            fi
            ;;
        -h|--help|help)
            usage
            ;;
        *)
            log "❌ Unknown command: $command"
            usage
            exit 1
            ;;
    esac
}

main "$@"
