#!/usr/bin/env python3
import os
import re
import sys


def main():
    if len(sys.argv) < 2:
        print("Usage: devbox run race-recorder:bump-version <major|minor|bug>")
        print("Example: devbox run race-recorder:bump-version minor")
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

    # Locate franklin-race-recorder.py
    target_path = "franklin-race-recorder.py"
    if not os.path.exists(target_path):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        target_path = os.path.join(script_dir, "../franklin-race-recorder.py")
        if not os.path.exists(target_path):
            print(f"Error: {target_path} not found.")
            sys.exit(1)

    with open(target_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Find VERSION = "X.Y.Z"
    pattern = r"(VERSION\s*=\s*\")([0-9]+)\.([0-9]+)\.([0-9]+)(\")"
    match = re.search(pattern, content)
    if not match:
        print('Error: Could not find VERSION = "X.Y.Z" in franklin-race-recorder.py.')
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
    matched_full = match.group(0)
    replaced_full = f"{prefix}{new_version}{suffix}"
    updated_content = content.replace(matched_full, replaced_full)

    with open(target_path, "w", encoding="utf-8") as f:
        f.write(updated_content)

    print(f"✓ Version bumped from {old_version} to {new_version} in {target_path}")


if __name__ == "__main__":
    main()
