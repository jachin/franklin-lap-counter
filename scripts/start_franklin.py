#!/usr/bin/env python3
"""Systemd wrapper for Franklin Lap Counter services.

Replaces the old tmuxinator-based startup. All services are managed as
systemd units under franklin.target.
"""

import os
import shutil
import subprocess
import sys
from datetime import datetime


FRANKLIN_TARGET = "franklin.target"


def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def check_systemd() -> bool:
    if not shutil.which("systemctl"):
        log("❌ systemctl not found. This script requires systemd.")
        return False
    return True


def run_systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl"] + list(args), capture_output=True, text=True)


def start_franklin():
    log("Starting Franklin Lap Counter services...")
    result = run_systemctl("start", FRANKLIN_TARGET)
    if result.returncode == 0:
        log("✓ Franklin services started")
        show_status_summary()
    else:
        log(f"❌ Failed to start: {result.stderr.strip()}")
        sys.exit(1)


def stop_franklin():
    log("Stopping Franklin Lap Counter services...")
    result = run_systemctl("stop", FRANKLIN_TARGET)
    if result.returncode == 0:
        log("✓ Franklin services stopped")
    else:
        log(f"❌ Failed to stop: {result.stderr.strip()}")
        sys.exit(1)


def restart_franklin():
    log("Restarting Franklin Lap Counter services...")
    result = run_systemctl("restart", FRANKLIN_TARGET)
    if result.returncode == 0:
        log("✓ Franklin services restarted")
        show_status_summary()
    else:
        log(f"❌ Failed to restart: {result.stderr.strip()}")
        sys.exit(1)


def status_franklin():
    result = run_systemctl("is-active", FRANKLIN_TARGET)
    is_active = result.stdout.strip() == "active"

    if is_active:
        log(f"✓ {FRANKLIN_TARGET} is active")
    else:
        log(f"❌ {FRANKLIN_TARGET} is not active")

    print()
    result = run_systemctl("status", "--no-pager", FRANKLIN_TARGET)
    for line in result.stdout.splitlines():
        print(f"  {line}")
    if result.stderr.strip():
        for line in result.stderr.splitlines():
            print(f"  {line}")

    print()
    result = run_systemctl(
        "list-units", "--no-pager", "franklin-*", "--all"
    )
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    for line in lines:
        print(f"  {line}")


def show_status_summary():
    result = run_systemctl("is-active", "--quiet", FRANKLIN_TARGET)
    if result.returncode == 0:
        print("  Active services:")
        result = run_systemctl(
            "list-units", "--no-pager", "franklin-*", "--all"
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[1] in ("loaded",):
                status = "✓" if parts[2] == "active" else "❌"
                print(f"    {status} {parts[0]}")


def enable_franklin():
    log("Enabling Franklin services to start on boot...")
    result = run_systemctl("enable", FRANKLIN_TARGET)
    if result.returncode == 0:
        log("✓ Franklin services enabled on boot")
    else:
        log(f"❌ Failed to enable: {result.stderr.strip()}")
        sys.exit(1)


def disable_franklin():
    log("Disabling Franklin services on boot...")
    result = run_systemctl("disable", FRANKLIN_TARGET)
    if result.returncode == 0:
        log("✓ Franklin services disabled on boot")
    else:
        log(f"❌ Failed to disable: {result.stderr.strip()}")
        sys.exit(1)


def logs_franklin(service: str = "", follow: bool = False):
    if not service:
        service = "franklin.target"
    elif not service.startswith("franklin-"):
        service = f"franklin-{service}"

    cmd = ["journalctl", "-u", service, "--no-pager"]
    if follow:
        cmd.append("-f")
    os.execvp("journalctl", cmd)


def usage():
    print(f"Usage: {sys.argv[0]} <command> [options]")
    print()
    print("Commands:")
    print("  start           Start all Franklin services")
    print("  stop            Stop all Franklin services")
    print("  restart         Restart all Franklin services")
    print("  status          Show status of all services")
    print("  enable          Enable services on boot")
    print("  disable         Disable services on boot")
    print("  logs [service]  View logs (default: all)")
    print("  logs -f         Follow logs (tail -f)")
    print()
    print("Examples:")
    print("  {} start".format(sys.argv[0]))
    print("  {} status".format(sys.argv[0]))
    print("  {} logs hardware-monitor -f".format(sys.argv[0]))
    print("  {} logs race-recorder".format(sys.argv[0]))


def main():
    if not check_systemd():
        sys.exit(1)

    command = sys.argv[1] if len(sys.argv) > 1 else ""

    if command in ("start", ""):
        enable_franklin()
        start_franklin()
    elif command == "stop":
        stop_franklin()
    elif command == "restart":
        restart_franklin()
    elif command == "status":
        status_franklin()
    elif command == "enable":
        enable_franklin()
    elif command == "disable":
        disable_franklin()
    elif command == "logs":
        follow = "-f" in sys.argv
        svc_args = [a for a in sys.argv[2:] if a != "-f"]
        svc = svc_args[0] if svc_args else ""
        logs_franklin(svc, follow)
    elif command in ("-h", "--help", "help"):
        usage()
    else:
        log(f"❌ Unknown command: {command}")
        usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
