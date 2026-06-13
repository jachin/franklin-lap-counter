#!/usr/bin/env python3
import os
import subprocess
import sys
import time


def main():
    # Navigate to the project root (parent directory of scripts/)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)
    os.chdir(project_root)

    hw_proc = None
    web_proc = None

    try:
        print("Building Rust project...")
        subprocess.run(
            ["cargo", "build", "--manifest-path", "rust/Cargo.toml"], check=True
        )

        print("Starting hardware simulator...")
        hw_proc = subprocess.Popen(
            [
                "cargo",
                "run",
                "--manifest-path",
                "rust/Cargo.toml",
                "--bin",
                "franklin-hardware-monitor",
                "--",
                "--sim",
            ]
        )

        print("Starting web server...")
        web_proc = subprocess.Popen([sys.executable, "scoreboard_web_app.py"])

        # Small delay to let background services start up
        time.sleep(1)

        print("Starting Franklin in sim mode (race UI)...")
        # Run TUI in the foreground
        subprocess.run([sys.executable, "franklin-tui.py", "--race"])

    except KeyboardInterrupt:
        pass
    except subprocess.CalledProcessError as e:
        print(f"❌ Subprocess failed: {e}", file=sys.stderr)
    finally:
        # Clean up processes
        for proc, name in [(web_proc, "web server"), (hw_proc, "hardware simulator")]:
            if proc is not None and proc.poll() is None:
                print(f"\nStopping {name} (pid {proc.pid})...")
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    print(f"Force-killing {name} (pid {proc.pid})...")
                    proc.kill()
                    proc.wait()


if __name__ == "__main__":
    main()
