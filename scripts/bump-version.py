#!/usr/bin/env python3
import os
import re
import sys


def main():
    if len(sys.argv) < 2:
        print("Usage: devbox run hardware-monitor:bump-version <major|minor|bug>")
        print("Example: devbox run hardware-monitor:bump-version minor")
        sys.exit(1)

    action = sys.argv[1].lower()
    # Normalize 'bug' to 'patch' internally
    if action == "bug":
        action = "patch"

    if action not in ["major", "minor", "patch"]:
        print(
            "Error: Invalid argument. Must be 'major', 'minor', or 'bug' (or 'patch')."
        )
        sys.exit(1)

    # Locate Cargo.toml
    # Since we run inside devbox, the working directory is the project root.
    cargo_toml_path = "rust/Cargo.toml"
    if not os.path.exists(cargo_toml_path):
        # Try parent directory relative to script
        script_dir = os.path.dirname(os.path.abspath(__file__))
        cargo_toml_path = os.path.join(script_dir, "../rust/Cargo.toml")
        if not os.path.exists(cargo_toml_path):
            print(f"Error: {cargo_toml_path} not found.")
            sys.exit(1)

    with open(cargo_toml_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Find [workspace.package] and the version under it
    pattern = r"(\[workspace\.package\]\s*[\s\S]*?version\s*=\s*\")([0-9]+)\.([0-9]+)\.([0-9]+)(\")"
    match = re.search(pattern, content)
    if not match:
        print(
            "Error: Could not find version inside [workspace.package] in rust/Cargo.toml."
        )
        sys.exit(1)

    prefix, major_str, minor_str, patch_str, suffix = match.groups()
    major = int(major_str)
    minor = int(minor_str)
    patch = int(patch_str)
    old_version = f"{major}.{minor}.{patch}"

    if action == "major":
        major += 1
        minor = 0
        patch = 0
    elif action == "minor":
        minor += 1
        patch = 0
    elif action == "patch":
        patch += 1

    new_version = f"{major}.{minor}.{patch}"

    # Replace in content
    # Re-construct the exact matched substring to replace it precisely
    matched_full = match.group(0)
    replaced_full = f"{prefix}{new_version}{suffix}"
    updated_content = content.replace(matched_full, replaced_full)

    with open(cargo_toml_path, "w", encoding="utf-8") as f:
        f.write(updated_content)

    print(f"✓ Version bumped from {old_version} to {new_version} in {cargo_toml_path}")


if __name__ == "__main__":
    main()
