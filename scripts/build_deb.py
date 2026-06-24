#!/usr/bin/env python3
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime


def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def main():
    # Ensure we are in the project root
    # (dirname of scripts/build_deb.py is scripts, parent is project root)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)
    os.chdir(project_root)

    # Get the current version from rust/Cargo.toml
    cargo_toml_path = "rust/Cargo.toml"
    if not os.path.exists(cargo_toml_path):
        log(f"❌ Cannot find Cargo.toml at {cargo_toml_path}")
        sys.exit(1)

    with open(cargo_toml_path, "r") as f:
        cargo_content = f.read()

    match = re.search(
        r'\[workspace\.package\]\s*[\s\S]*?version\s*=\s*"([^"]+)"', cargo_content
    )
    if not match:
        log(f"❌ Could not extract version from {cargo_toml_path}")
        sys.exit(1)

    version = match.group(1)
    log(f"Hardware monitor version detected: {version}")

    binary_path = (
        "rust/target/aarch64-unknown-linux-gnu/release/franklin-hardware-monitor"
    )

    # Always (re)build the Pi binary before packaging so the .deb can never ship
    # a stale binary. cargo is incremental, so this is cheap when nothing changed
    # but guarantees source edits are picked up even when an older binary already
    # exists at binary_path.
    log("Building Pi binary before packaging...")
    try:
        subprocess.run([sys.executable, "scripts/rust_pi_build.py"], check=True)
    except subprocess.CalledProcessError:
        log("❌ Local cross-build failed. Cannot build Debian package.")
        sys.exit(1)

    if not os.path.exists(binary_path):
        log(f"❌ Build did not produce expected binary at {binary_path}")
        sys.exit(1)

    # Create package directory structure
    pkg_name = "franklin-hardware-monitor"
    pkg_dir = f"rust/target/debian/{pkg_name}_{version}_arm64"
    deb_file = f"rust/target/debian/{pkg_name}_{version}_arm64.deb"

    log(f"Preparing package directory at {pkg_dir}...")
    if os.path.exists(pkg_dir):
        shutil.rmtree(pkg_dir)

    os.makedirs(f"{pkg_dir}/usr/bin", exist_ok=True)
    os.makedirs(f"{pkg_dir}/DEBIAN", exist_ok=True)

    # Copy binary
    dest_binary = f"{pkg_dir}/usr/bin/franklin-hardware-monitor"
    shutil.copy(binary_path, dest_binary)
    os.chmod(dest_binary, 0o755)

    # Generate control file
    control_content = f"""Package: {pkg_name}
Version: {version}
Section: utils
Priority: optional
Architecture: arm64
Maintainer: Jachin Rupe <jachin@jachin.rupe.name>
Depends: libudev1 | libudev-dev
Description: Franklin Hardware Monitor
 Hardware monitor service for the Franklin RC Car Lap Counter.
 Connects to the local hardware/serial ports and publishes events to Redis.
"""

    control_file_path = f"{pkg_dir}/DEBIAN/control"
    with open(control_file_path, "w") as f:
        f.write(control_content)

    os.chmod(control_file_path, 0o644)

    # Build the package
    log("Building package using dpkg-deb...")
    try:
        subprocess.run(
            ["dpkg-deb", "--root-owner-group", "--build", pkg_dir, deb_file],
            check=True,
        )
    except subprocess.CalledProcessError as e:
        log(f"❌ dpkg-deb failed: {e}")
        sys.exit(1)
    except FileNotFoundError:
        log("❌ 'dpkg-deb' command not found. Is dpkg installed?")
        sys.exit(1)

    log("✓ Debian package built successfully!")
    log(f"  Package location: {deb_file}")

    if shutil.which("file"):
        try:
            info = subprocess.check_output(["file", deb_file], text=True).strip()
            log(f"  Package info: {info}")
        except subprocess.SubprocessError:
            pass

    if os.path.exists(deb_file):
        size_bytes = os.path.getsize(deb_file)
        # Format human-readable size
        for unit in ["B", "KiB", "MiB", "GiB"]:
            if size_bytes < 1024.0:
                size_str = f"{size_bytes:.1f} {unit}"
                break
            size_bytes /= 1024.0
        else:
            size_str = f"{size_bytes:.1f} TiB"
        log(f"  Package size: {size_str}")


if __name__ == "__main__":
    main()
