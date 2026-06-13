#!/usr/bin/env python3
import os
import shutil
import subprocess
import sys
from datetime import datetime


def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def main():
    # Load .env file manually if it exists to mimic 'source .env'
    if os.path.exists(".env"):
        with open(".env", "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    # Strip quotes if present
                    val = val.strip("'\"")
                    os.environ[key] = val

    rust_target = os.environ.get(
        "RUST_PI_TARGET", os.environ.get("RUST_TARGET", "aarch64-unknown-linux-gnu")
    )

    log("Building Rust binary for Raspberry Pi...")
    log(f"Target architecture: {rust_target}")

    if not shutil.which("rustup"):
        log("❌ rustup not found")
        sys.exit(1)

    # Check if target is installed
    try:
        installed_targets = subprocess.check_output(
            ["rustup", "target", "list", "--installed"], text=True
        ).splitlines()
    except subprocess.SubprocessError as e:
        log(f"❌ Failed to query installed targets: {e}")
        sys.exit(1)

    if rust_target not in [t.strip() for t in installed_targets]:
        log(f"Installing Rust target: {rust_target}")
        try:
            subprocess.run(["rustup", "target", "add", rust_target], check=True)
        except subprocess.CalledProcessError:
            log(f"❌ Failed to install Rust target: {rust_target}")
            sys.exit(1)

    build_cmd = "cargo"
    if shutil.which("cross"):
        log(
            "✓ 'cross' tool detected! Using containerized cross-compilation with 'cross'..."
        )
        build_cmd = "cross"

    log(f"Running build with {build_cmd}...")
    try:
        subprocess.run(
            [
                build_cmd,
                "build",
                "--release",
                "--manifest-path",
                "rust/Cargo.toml",
                "--target",
                rust_target,
            ],
            check=True,
        )
    except subprocess.CalledProcessError:
        log(f"❌ Cross-build failed for {rust_target}")
        log("   The Rust hardware monitor depends on libudev, so compiling for Linux")
        log(
            "   on a Mac requires a sysroot/cross-linker setup or a container-based build tool."
        )
        if build_cmd == "cargo":
            log("")
            log(
                "   💡 Recommendation: Install and use 'cross' to build seamlessly inside a Docker container:"
            )
            log(
                "      1. Install cross:  cargo install cross --git https://github.com/cross-rs/cross"
            )
            log("      2. Start Docker")
            log("      3. Run this build task again")
        sys.exit(1)

    binary_path = f"rust/target/{rust_target}/release/franklin-hardware-monitor"
    if os.path.exists(binary_path):
        log("✓ Build successful")
        log(f"  Binary location: {binary_path}")
        size_bytes = os.path.getsize(binary_path)
        # Format human-readable size
        for unit in ["B", "KiB", "MiB", "GiB"]:
            if size_bytes < 1024.0:
                size_str = f"{size_bytes:.1f} {unit}"
                break
            size_bytes /= 1024.0
        else:
            size_str = f"{size_bytes:.1f} TiB"
        log(f"  Binary size: {size_str}")

        if shutil.which("file"):
            try:
                info = subprocess.check_output(["file", binary_path], text=True).strip()
                log(f"  Binary info: {info}")
            except subprocess.SubprocessError:
                pass
    else:
        log(f"❌ Build failed - binary not found at {binary_path}")
        sys.exit(1)


if __name__ == "__main__":
    main()
