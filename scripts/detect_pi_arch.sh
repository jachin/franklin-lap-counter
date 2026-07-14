#!/usr/bin/env bash
# Detect the Raspberry Pi's Debian architecture and print the corresponding
# Rust target triple to stdout.  Used by `devbox run deploy` so the .deb
# package always matches the Pi even if it switches between 32/64-bit.
#
# Exit codes:
#   0 – target printed to stdout
#   1 – ansible failed or architecture not recognised

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
INVENTORY="$PROJECT_ROOT/playbooks/inventory.ini"

if [[ ! -f "$INVENTORY" ]]; then
    echo "Error: inventory not found at $INVENTORY" >&2
    exit 1
fi

ARCH=$(ansible all -i "$INVENTORY" -m shell -a 'dpkg --print-architecture' --one-line 2>/dev/null \
    | awk '{print $NF}' \
    | tr -d '[:space:]')

case "$ARCH" in
    arm64)  echo "aarch64-unknown-linux-gnu" ;;
    armhf)  echo "armv7-unknown-linux-gnueabihf" ;;
    *)
        echo "Error: unsupported Pi architecture '$ARCH' (expected arm64 or armhf)" >&2
        exit 1
        ;;
esac
