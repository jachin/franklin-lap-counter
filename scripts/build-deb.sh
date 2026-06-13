#!/bin/bash
# Build Debian package for Raspberry Pi Bookworm (arm64)

set -euo pipefail

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

# 1. Ensure we are in the project root
cd "$(dirname "$0")/.."

# 2. Get the current version from rust/Cargo.toml
VERSION=$(python3 -c "import re; m = re.search(r'\[workspace\.package\]\s*[\s\S]*?version\s*=\s*\"([^\"]+)\"', open('rust/Cargo.toml').read()); print(m.group(1))")
log "Hardware monitor version detected: $VERSION"

BINARY_PATH="rust/target/aarch64-unknown-linux-gnu/release/franklin-hardware-monitor"

# 3. Ensure the binary is built
if [ ! -f "$BINARY_PATH" ]; then
    log "Binary not found at $BINARY_PATH. Running build..."
    if ! ./scripts/rust-pi-build.sh; then
        log "❌ Local cross-build failed. Cannot build Debian package."
        exit 1
    fi
fi

# 4. Create package directory structure
PKG_NAME="franklin-hardware-monitor"
PKG_DIR="rust/target/debian/${PKG_NAME}_${VERSION}_arm64"
DEB_FILE="rust/target/debian/${PKG_NAME}_${VERSION}_arm64.deb"

log "Preparing package directory at $PKG_DIR..."
rm -rf "$PKG_DIR"
mkdir -p "$PKG_DIR/usr/bin"
mkdir -p "$PKG_DIR/DEBIAN"

# Copy binary
cp "$BINARY_PATH" "$PKG_DIR/usr/bin/"
chmod 755 "$PKG_DIR/usr/bin/franklin-hardware-monitor"

# 5. Generate control file
cat << EOF > "$PKG_DIR/DEBIAN/control"
Package: $PKG_NAME
Version: $VERSION
Section: utils
Priority: optional
Architecture: arm64
Maintainer: Jachin Rupe <jachin@jachin.rupe.name>
Depends: libudev1 | libudev-dev
Description: Franklin Hardware Monitor
 Hardware monitor service for the Franklin RC Car Lap Counter.
 Connects to the local hardware/serial ports and publishes events to Redis.
EOF

chmod 644 "$PKG_DIR/DEBIAN/control"

# 6. Build the package
if command -v dpkg-deb >/dev/null 2>&1; then
    log "✓ Local dpkg-deb found. Building package..."
    dpkg-deb --build "$PKG_DIR" "$DEB_FILE"
elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    log "✓ Docker found and running. Building package using debian:bookworm-slim container..."
    # Ensure correct root permissions inside the tarball by running dpkg-deb in docker
    docker run --rm -v "$(pwd)":/workspace -w /workspace debian:bookworm-slim dpkg-deb --build "$PKG_DIR" "$DEB_FILE"
else
    log "⚠ Neither 'dpkg-deb' nor a running Docker daemon was found."
    log "  Falling back to pure Python Debian package generator..."
    python3 scripts/build-deb-pure.py
fi

log "✓ Debian package built successfully!"
log "  Package location: $DEB_FILE"
if command -v file >/dev/null 2>&1; then
    log "  Package info: $(file "$DEB_FILE")"
fi
ls -lh "$DEB_FILE"
