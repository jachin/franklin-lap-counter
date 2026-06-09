#!/bin/bash
# Build Rust binary for Raspberry Pi target without containers

set -euo pipefail

# Load .env file if it exists
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

RUST_TARGET="${RUST_PI_TARGET:-${RUST_TARGET:-aarch64-unknown-linux-gnu}}"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

main() {
    log "Building Rust binary for Raspberry Pi..."
    log "Target architecture: $RUST_TARGET"

    if ! command -v rustup >/dev/null 2>&1; then
        log "❌ rustup not found"
        exit 1
    fi

    if ! rustup target list --installed | grep -qx "$RUST_TARGET"; then
        log "Installing Rust target: $RUST_TARGET"
        rustup target add "$RUST_TARGET"
    fi

    cargo build --release --manifest-path rust/Cargo.toml --target "$RUST_TARGET"

    BINARY_PATH="rust/target/$RUST_TARGET/release/franklin-hardware-monitor"
    if [ -f "$BINARY_PATH" ]; then
        log "✓ Build successful"
        log "  Binary location: $BINARY_PATH"
        log "  Binary size: $(ls -lh "$BINARY_PATH" | awk '{print $5}')"
        if command -v file >/dev/null 2>&1; then
            log "  Binary info: $(file "$BINARY_PATH")"
        fi
    else
        log "❌ Build failed - binary not found at $BINARY_PATH"
        exit 1
    fi
}

main "$@"
