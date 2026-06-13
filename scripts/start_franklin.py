#!/usr/bin/env python3
import os
import shutil
import subprocess
import sys
from datetime import datetime


def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def check_dependencies() -> bool:
    missing = []
    if not shutil.which("tmux"):
        missing.append("tmux")
    if not shutil.which("tmuxinator"):
        missing.append("tmuxinator")

    if missing:
        log(f"❌ Missing dependencies: {' '.join(missing)}")
        log("   Please install them first:")
        log("   sudo apt-get install tmux")
        log("   sudo gem install tmuxinator")
        return False
    return True


def check_files(tmuxinator_config: str) -> bool:
    missing = []
    if not os.path.isfile(tmuxinator_config):
        missing.append(tmuxinator_config)

    # Check for hardware monitor either locally or in system PATH
    has_hw_monitor = os.path.isfile("franklin-hardware-monitor") or shutil.which(
        "franklin-hardware-monitor"
    )
    if not has_hw_monitor:
        missing.append("franklin-hardware-monitor")

    for f in [
        "franklin-tui.py",
        "gui_config.py",
        "redis_commands.py",
        "scoreboard_web_app.py",
        "referee_web_app.py",
        "healthcheck_web_app.py",
    ]:
        if not os.path.isfile(f):
            missing.append(f)

    if not os.path.isdir(".venv"):
        missing.append(".venv (Python virtual environment)")

    if missing:
        log(f"❌ Missing files: {', '.join(missing)}")
        log("   Run deployment first: devbox run ansible:deploy")
        return False
    return True


def stop_franklin(session_name: str):
    log("Stopping Franklin Lap Counter...")

    # Check if tmux session exists
    if shutil.which("tmux"):
        res = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True,
        )
        if res.returncode == 0:
            log(f"Killing tmux session: {session_name}")
            subprocess.run(["tmux", "kill-session", "-t", session_name])
        else:
            log("No running session found")

    # Clean up any remaining processes
    # Using pkill via subprocess
    subprocess.run(["pkill", "-f", "franklin-hardware-monitor"], capture_output=True)
    subprocess.run(["pkill", "-f", "redis-server.*redis.sock"], capture_output=True)

    # Clean up socket file
    if os.path.exists("redis.sock"):
        try:
            os.remove("redis.sock")
        except Exception as e:
            log(f"Error removing redis.sock: {e}")

    log("✓ Franklin Lap Counter stopped")


def start_franklin(session_name: str, tmuxinator_config: str):
    log("Starting Franklin Lap Counter with tmuxinator...")
    os.environ["TMUX_SESSION_NAME"] = session_name

    # Check if session exists
    res = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True,
    )
    if res.returncode == 0:
        log(f"⚠ Session '{session_name}' already exists")
        ans = input("Kill existing session and restart? (y/N): ").strip().lower()
        if ans in ["y", "yes"]:
            stop_franklin(session_name)
        else:
            log("Selecting lap-counter window and attaching to existing session...")
            subprocess.run(
                ["tmux", "select-window", "-t", f"{session_name}:lap-counter"],
                capture_output=True,
            )
            subprocess.run(["tmux", "attach-session", "-t", session_name])
            return

    log(f"Starting tmux session with configuration: {tmuxinator_config}")
    try:
        subprocess.run(["tmuxinator", "start", "-p", tmuxinator_config], check=True)
    except subprocess.CalledProcessError as e:
        log(f"❌ Failed to start tmuxinator: {e}")
        sys.exit(1)


def status_franklin(session_name: str):
    log("Franklin Lap Counter Status:")

    # Check tmux session
    res = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True,
    )
    if res.returncode == 0:
        log(f"✓ Tmux session '{session_name}' is running")
        log("Active windows:")
        # List windows and indent output
        windows = subprocess.check_output(
            ["tmux", "list-windows", "-t", session_name],
            text=True,
        )
        for line in windows.splitlines():
            print(f"  {line}")
    else:
        log("❌ No tmux session found")

    print("\nProcess status:")

    # Helper function to check if process is running
    def check_proc(pattern: str) -> bool:
        try:
            # -f matches full command line
            subprocess.check_output(["pgrep", "-f", pattern])
            return True
        except subprocess.CalledProcessError:
            return False

    status_map = [
        ("redis-server.*redis.sock", "Redis server"),
        ("franklin-hardware-monitor", "Hardware monitor"),
        ("franklin-tui.py", "Franklin TUI"),
        ("scoreboard_web_app.py", "Scoreboard web server"),
        ("referee_web_app.py", "Referee web server"),
        ("healthcheck_web_app.py", "Health check web server"),
    ]

    for pattern, name in status_map:
        if check_proc(pattern):
            log(f"  ✓ {name} running")
        else:
            log(f"  ❌ {name} not running")


def attach_franklin(session_name: str):
    res = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True,
    )
    if res.returncode == 0:
        log(f"Selecting lap-counter window and attaching to {session_name} session...")
        subprocess.run(
            ["tmux", "select-window", "-t", f"{session_name}:lap-counter"],
            capture_output=True,
        )
        subprocess.run(["tmux", "attach-session", "-t", session_name])
    else:
        log(f"❌ No {session_name} session found. Start it first.")
        sys.exit(1)


def usage():
    print(f"Usage: {sys.argv[0]} [start|stop|status|attach|restart]")
    print()
    print("Commands:")
    print("  start   - Start Franklin Lap Counter with tmuxinator")
    print("  stop    - Stop all Franklin processes and tmux session")
    print("  status  - Show status of Franklin components")
    print("  attach  - Attach to existing tmux session")
    print("  restart - Stop and start Franklin")
    print()
    print("If no command is provided, 'start' is assumed.")


def main():
    # Load .env if it exists
    if os.path.exists(".env"):
        with open(".env", "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    val = val.strip("'\"")
                    os.environ[key] = val

    # cd to the script's directory (which should be the project root when deployed)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)

    session_name = os.environ.get("TMUX_SESSION_NAME", "franklin")
    tmuxinator_config = "tmuxinator/franklin.yml"

    command = sys.argv[1] if len(sys.argv) > 1 else "start"

    if command == "start":
        log("Franklin Lap Counter Startup")
        if not check_dependencies() or not check_files(tmuxinator_config):
            sys.exit(1)
        start_franklin(session_name, tmuxinator_config)
    elif command == "stop":
        stop_franklin(session_name)
    elif command == "status":
        status_franklin(session_name)
    elif command == "attach":
        attach_franklin(session_name)
    elif command == "restart":
        stop_franklin(session_name)
        time_sleep = 2
        # Use import time if we sleep
        import time

        time.sleep(time_sleep)
        if check_dependencies() and check_files(tmuxinator_config):
            start_franklin(session_name, tmuxinator_config)
        else:
            sys.exit(1)
    elif command in ["-h", "--help", "help"]:
        usage()
    else:
        log(f"❌ Unknown command: {command}")
        usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
