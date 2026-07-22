#!/usr/bin/env python3
"""Launch the Franklin GTK GUI in a sway/Wayland session.

Waits for systemd background services (franklin.target) to be ready,
then launches the GTK GUI in a restart loop so it auto-recovers from crashes.

Intended to be called from sway config:
  exec /opt/franklin-lap-counter/start_franklin_gui_session
"""

import os
import subprocess
import sys
import time
from datetime import datetime

APP_DIR = "/opt/franklin-lap-counter"
VENV_PYTHON = f"{APP_DIR}/.venv/bin/python"
GUI_SCRIPT = f"{APP_DIR}/franklin-gui.py"
GUI_LOG = "/var/log/franklin/franklin-gui.log"
REDIS_SOCKET = f"{APP_DIR}/run/redis.sock"

FRANKLIN_TARGET = "franklin.target"
STARTUP_TIMEOUT = 30


def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def wait_for_services() -> bool:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", FRANKLIN_TARGET],
            capture_output=True,
        )
        if result.returncode == 0:
            log("Background services are ready")
            return True
        time.sleep(1)
    log(f"Timed out waiting for {FRANKLIN_TARGET}")
    return False


def run_gui():
    os.chdir(APP_DIR)
    os.makedirs(os.path.dirname(GUI_LOG), exist_ok=True)

    python_bin = VENV_PYTHON if os.path.isfile(VENV_PYTHON) else "python3"
    restart_delay = 1

    while True:
        log("Starting Franklin GTK GUI...")
        try:
            with open(GUI_LOG, "a") as log_f:
                gui_env = {**os.environ, "FRANKLIN_REDIS_SOCKET": REDIS_SOCKET}
                result = subprocess.run(
                    [python_bin, GUI_SCRIPT],
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    env=gui_env,
                )
            if result.returncode == 0:
                log("GUI exited normally")
                break
            else:
                log(f"GUI crashed (exit code {result.returncode}), restarting in {restart_delay}s...")
        except FileNotFoundError as e:
            log(f"Cannot start GUI: {e}")
            break
        except Exception as e:
            log(f"Unexpected error: {e}")
            break

        time.sleep(restart_delay)
        restart_delay = min(restart_delay * 2, 30)


def main():
    log("Franklin GUI session starting...")

    if not os.path.isdir(APP_DIR):
        log(f"Franklin app directory not found at {APP_DIR}")
        sys.exit(1)

    if not wait_for_services():
        log("Proceeding to launch GUI anyway...")

    run_gui()


if __name__ == "__main__":
    main()
