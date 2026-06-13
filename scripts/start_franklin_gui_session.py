#!/usr/bin/env python3
import os
import shutil
import subprocess
import sys
from datetime import datetime


def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def cleanup(session_name: str):
    # Check if tmux session exists
    if shutil.which("tmux"):
        try:
            res = subprocess.run(
                ["tmux", "has-session", "-t", session_name],
                capture_output=True,
            )
            if res.returncode == 0:
                log(f"Stopping tmux session: {session_name}")
                subprocess.run(["tmux", "kill-session", "-t", session_name])
        except Exception as e:
            log(f"Error checking/stopping tmux session: {e}")

    # Remove socket
    if os.path.exists("./redis.sock"):
        try:
            os.remove("./redis.sock")
        except Exception as e:
            log(f"Error removing redis.sock: {e}")


def main():
    # cd to the script's directory (which is where it resides)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)

    session_name = os.environ.get("TMUX_SESSION_NAME", "franklin-services")
    tmuxinator_config = os.environ.get(
        "TMUXINATOR_CONFIG", "tmuxinator/franklin-services.yml"
    )

    try:
        # Create log files if they don't exist
        for log_file in ["gui.log", "hardware_redis.log", "redis.log", "web.log"]:
            with open(log_file, "a"):
                os.utime(log_file, None)

        if not os.path.exists("gui_config.py"):
            log(f"❌ Missing gui_config.py in {os.getcwd()}")
            sys.exit(1)

        if not os.path.exists("redis_commands.py"):
            log(f"❌ Missing redis_commands.py in {os.getcwd()}")
            sys.exit(1)

        if not shutil.which("tmux"):
            log("❌ tmux not found")
            sys.exit(1)

        if not shutil.which("tmuxinator"):
            log("❌ tmuxinator not found")
            sys.exit(1)

        if not os.path.exists(tmuxinator_config):
            log(f"❌ Missing tmuxinator config: {tmuxinator_config}")
            sys.exit(1)

        # Check tmux session
        res = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True,
        )
        if res.returncode == 0:
            log(f"Tmux session '{session_name}' already running; reusing it")
        else:
            log(f"Starting tmux services via {tmuxinator_config}")
            try:
                subprocess.run(
                    ["tmuxinator", "start", "-p", tmuxinator_config, "--no-attach"],
                    check=True,
                )
            except subprocess.CalledProcessError:
                log("⚠️ tmux services failed to start cleanly; continuing to launch GUI")

        log(
            "Starting Franklin GTK GUI (using saved mode preference unless CLI override is provided)..."
        )

        # Resolve correct python interpreter (prefer virtualenv python)
        python_bin = sys.executable
        if os.path.isdir(".venv"):
            venv_python = os.path.join(".venv", "bin", "python")
            if os.path.exists(venv_python):
                python_bin = venv_python

        # Run the GUI, appending output to gui.log
        with open("gui.log", "a") as log_f:
            subprocess.run(
                [python_bin, "franklin-gui.py"],
                stdout=log_f,
                stderr=subprocess.STDOUT,
            )

    except KeyboardInterrupt:
        pass
    finally:
        cleanup(session_name)


if __name__ == "__main__":
    main()
