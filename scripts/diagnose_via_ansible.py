#!/usr/bin/env python3
"""Run diagnose.sh on the Pi using the Ansible inventory for host/user."""

import os
import subprocess
import sys
import re

INVENTORY = "playbooks/inventory.ini"
DIAGNOSE_SCRIPT = "scripts/diagnose.sh"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
inventory_path = os.path.join(PROJECT_ROOT, INVENTORY)
diagnose_path = os.path.join(PROJECT_ROOT, DIAGNOSE_SCRIPT)

if not os.path.exists(inventory_path):
    print(f"ERROR: Inventory not found at {inventory_path}")
    print(f"Copy from {inventory_path}.example.ini and edit the host.")
    sys.exit(1)

if not os.path.exists(diagnose_path):
    print(f"ERROR: Diagnose script not found at {diagnose_path}")
    sys.exit(1)


def parse_inventory(path):
    """Return (host, user) from a simple Ansible inventory.ini.

    User defaults to None (SSH uses current local user) unless
    ``ansible_user`` is explicitly set in the inventory.
    """
    host = None
    user = None
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("["):
                continue
            parts = line.split()
            if not parts:
                continue
            host = parts[0]
            for part in parts[1:]:
                if "=" in part:
                    k, v = part.split("=", 1)
                    if k == "ansible_user":
                        user = v
            break
    return host, user


pi_host, pi_user = parse_inventory(inventory_path)

if not pi_host:
    print(f"ERROR: No host found in {inventory_path}")
    print("Example content:")
    print("  [pi]")
    print("  raspberrypi.local")
    sys.exit(1)

if pi_user:
    ssh_dest = f"{pi_user}@{pi_host}"
else:
    ssh_dest = pi_host

print(f"Inventory host: {pi_host}")
print(f"SSH user:       {pi_user or '(current local user — no ansible_user set)'}")
print(f"Connecting:     ssh {ssh_dest}")
print(f"Running diagnose.sh...")
print()

with open(diagnose_path) as f:
    script_content = f.read()

cmd = ["ssh", ssh_dest, "sudo bash -s"]
result = subprocess.run(cmd, input=script_content, text=True)
sys.exit(result.returncode)
