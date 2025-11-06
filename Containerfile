# Rust version gets set as an enviormental varaible
# in `.env` (this value is just the default)
ARG RUST_VERSION=1.91
FROM rust:${RUST_VERSION}-bookworm

# Add ARM64 target for Raspberry Pi
RUN rustup target add aarch64-unknown-linux-gnu

# Install cross-compilation toolchain and dependencies
RUN apt-get update && \
    apt-get install -y \
    gcc-aarch64-linux-gnu \
    g++-aarch64-linux-gnu \
    pkg-config \
    libudev-dev \
    && rm -rf /var/lib/apt/lists/*

# Set up environment for cross-compilation
ENV CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc \
    CC_aarch64_unknown_linux_gnu=aarch64-linux-gnu-gcc \
    CXX_aarch64_unknown_linux_gnu=aarch64-linux-gnu-g++ \
    PKG_CONFIG_ALLOW_CROSS=1

WORKDIR /project

CMD ["/bin/bash"]
