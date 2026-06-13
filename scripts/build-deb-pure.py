#!/usr/bin/env python3
import io
import os
import re
import sys
import tarfile
import time


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def make_ar_header(name, size, mode=0o100644):
    """Generates a 60-byte BSD/GNU ar file header."""
    header = bytearray(60)

    # Format fields left-aligned, space-padded
    name_field = f"{name}".encode("ascii")[:16]
    date_field = f"{int(time.time())}".encode("ascii")[:12]
    uid_field = b"0"
    gid_field = b"0"
    mode_field = f"{oct(mode)[2:]}".encode("ascii")[:8]
    size_field = f"{size}".encode("ascii")[:10]

    header[0 : len(name_field)] = name_field
    header[16 : 16 + len(date_field)] = date_field
    header[28 : 28 + len(uid_field)] = uid_field
    header[34 : 34 + len(gid_field)] = gid_field
    header[40 : 40 + len(mode_field)] = mode_field
    header[48 : 48 + len(size_field)] = size_field

    # Fill empty spaces with ASCII spaces (0x20)
    for i in range(58):
        if header[i] == 0:
            header[i] = 0x20

    header[58] = 0x60
    header[59] = 0x0A
    return bytes(header)


def main():
    # Ensure working directory is project root
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(script_dir, ".."))
    os.chdir(project_root)

    cargo_toml_path = "rust/Cargo.toml"
    if not os.path.exists(cargo_toml_path):
        log(f"Error: {cargo_toml_path} not found.")
        sys.exit(1)

    with open(cargo_toml_path, "r", encoding="utf-8") as f:
        content = f.read()

    match = re.search(
        r"\[workspace\.package\]\s*[\s\S]*?version\s*=\s*\"([^\"]+)\"", content
    )
    if not match:
        log(
            "Error: Could not find version inside [workspace.package] in rust/Cargo.toml."
        )
        sys.exit(1)

    version = match.group(1)
    log(f"Hardware monitor version: {version}")

    binary_path = (
        "rust/target/aarch64-unknown-linux-gnu/release/franklin-hardware-monitor"
    )
    if not os.path.exists(binary_path):
        log(f"Binary not found at {binary_path}. Please compile it first.")
        sys.exit(1)

    # Output path
    output_dir = "rust/target/debian"
    os.makedirs(output_dir, exist_ok=True)
    deb_filename = f"{output_dir}/franklin-hardware-monitor_{version}_arm64.deb"

    # 1. Create control.tar.gz in memory
    control_content = f"""Package: franklin-hardware-monitor
Version: {version}
Section: utils
Priority: optional
Architecture: arm64
Maintainer: Jachin Rupe <jachin@jachin.rupe.name>
Depends: libudev1 | libudev-dev
Description: Franklin Hardware Monitor
 Hardware monitor service for the Franklin RC Car Lap Counter.
 Connects to the local hardware/serial ports and publishes events to Redis.
""".encode("utf-8")

    control_tar_io = io.BytesIO()
    # Use gzip compression, standard format
    with tarfile.open(
        fileobj=control_tar_io, mode="w:gz", format=tarfile.GNU_FORMAT
    ) as tar:
        tarinfo = tarfile.TarInfo(name="./control")
        tarinfo.size = len(control_content)
        tarinfo.mode = 0o644
        tarinfo.mtime = int(time.time())
        tarinfo.uid = 0
        tarinfo.gid = 0
        tarinfo.uname = "root"
        tarinfo.gname = "root"
        tar.addfile(tarinfo, io.BytesIO(control_content))

    control_tar_bytes = control_tar_io.getvalue()

    # 2. Create data.tar.gz in memory
    with open(binary_path, "rb") as f:
        binary_bytes = f.read()

    data_tar_io = io.BytesIO()
    with tarfile.open(
        fileobj=data_tar_io, mode="w:gz", format=tarfile.GNU_FORMAT
    ) as tar:
        # usr/ directory
        tarinfo = tarfile.TarInfo(name="./usr")
        tarinfo.type = tarfile.DIRTYPE
        tarinfo.mode = 0o755
        tarinfo.uid = 0
        tarinfo.gid = 0
        tarinfo.uname = "root"
        tarinfo.gname = "root"
        tar.addfile(tarinfo)

        # usr/bin/ directory
        tarinfo = tarfile.TarInfo(name="./usr/bin")
        tarinfo.type = tarfile.DIRTYPE
        tarinfo.mode = 0o755
        tarinfo.uid = 0
        tarinfo.gid = 0
        tarinfo.uname = "root"
        tarinfo.gname = "root"
        tar.addfile(tarinfo)

        # Binary
        tarinfo = tarfile.TarInfo(name="./usr/bin/franklin-hardware-monitor")
        tarinfo.size = len(binary_bytes)
        tarinfo.mode = 0o755
        tarinfo.uid = 0
        tarinfo.gid = 0
        tarinfo.uname = "root"
        tarinfo.gname = "root"
        tar.addfile(tarinfo, io.BytesIO(binary_bytes))

    data_tar_bytes = data_tar_io.getvalue()

    # 3. Write ar archive (.deb)
    log(f"Writing Debian package to {deb_filename}...")
    with open(deb_filename, "wb") as deb:
        # Global ar header
        deb.write(b"!<arch>\n")

        # Part 1: debian-binary
        binary_format = b"2.0\n"
        deb.write(make_ar_header("debian-binary", len(binary_format)))
        deb.write(binary_format)
        # No pad needed (even length of 4)

        # Part 2: control.tar.gz
        deb.write(make_ar_header("control.tar.gz", len(control_tar_bytes)))
        deb.write(control_tar_bytes)
        if len(control_tar_bytes) % 2 != 0:
            deb.write(b"\n")

        # Part 3: data.tar.gz
        deb.write(make_ar_header("data.tar.gz", len(data_tar_bytes)))
        deb.write(data_tar_bytes)
        if len(data_tar_bytes) % 2 != 0:
            deb.write(b"\n")

    log(f"✓ Pure-Python Debian package build successful!")
    log(f"  Package size: {os.path.getsize(deb_filename) / (1024 * 1024):.2f} MB")
    log(f"  Package file: {deb_filename}")


if __name__ == "__main__":
    main()
