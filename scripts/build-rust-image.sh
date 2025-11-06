#!/bin/bash
# Build a custom Rust cross-compilation image using environment variables

set -e

# Load .env file if it exists
# TODO This seems silly, like shouldn't devbox just do this for us?
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Use environment variables from devbox, with fallbacks
RUST_VERSION="${RUST_VERSION:-1.91}"
CONTAINER_IMAGE="${CONTAINER_IMAGE:-franklin-rust-builder}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "Building Rust cross-compilation container image..."
echo "  Rust version: $RUST_VERSION"
echo "  Image name: $CONTAINER_IMAGE:latest"

cd "$PROJECT_DIR"

# Build the container image with the Rust version as a build argument
container build \
  --build-arg RUST_VERSION="$RUST_VERSION" \
  --tag "$CONTAINER_IMAGE:latest" \
  --file Containerfile \
  .

echo "✓ Image built successfully: $CONTAINER_IMAGE:latest"
echo "  You can now run: devbox run rust-pi-build"
