#!/bin/bash
# Build Rust binary for Raspberry Pi using container

set -e

# Load .env file if it exists
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Configuration with defaults
CONTAINER_IMAGE="${CONTAINER_IMAGE:-franklin-rust-builder}"
RUST_TARGET="${RUST_PI_TARGET:-${RUST_TARGET:-aarch64-unknown-linux-gnu}}"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

main() {
    log "Building Rust binary for Raspberry Pi..."
    log "Container image: $CONTAINER_IMAGE:latest"
    log "Target architecture: $RUST_TARGET"

    # Check if container tool is available
    if ! command -v container &> /dev/null; then
        log "❌ Apple's 'container' tool not found"
        log "   Make sure Apple's container tool is installed and in PATH"
        exit 1
    fi

    # Get project directory
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

    log "Building in: $PROJECT_DIR"

    # Use Apple's container tool to build
    container run --rm \
      --volume "$PROJECT_DIR:/project" \
      "$CONTAINER_IMAGE:latest" \
      bash -c "cd /project && cargo build --release --manifest-path rust/Cargo.toml --target $RUST_TARGET"

    # Check if binary was created
    BINARY_PATH="rust/target/$RUST_TARGET/release/franklin-hardware-monitor"
    if [ -f "$BINARY_PATH" ]; then
        log "✓ Build successful!"
        log "  Binary location: $BINARY_PATH"

        # Show binary info
        log "  Binary size: $(ls -lh "$BINARY_PATH" | awk '{print $5}')"

        # Check if we can determine architecture
        if command -v file &> /dev/null; then
            ARCH_INFO=$(file "$BINARY_PATH" | grep -o "ARM aarch64\|ARM 64-bit\|x86-64" || echo "unknown")
            log "  Architecture: $ARCH_INFO"
        fi
    else
        log "❌ Build failed - binary not found at $BINARY_PATH"
        exit 1
    fi
}

main "$@"
