# Franklin 32-bit TUI Deployment Guide

This document describes deploying Franklin Lap Counter on **Raspberry Pi OS Lite 32-bit (armhf)** using the Textual TUI instead of the GTK4 GUI.

## Why 32-bit TUI?

| Metric | 64-bit GUI (GTK4 + sway) | 32-bit TUI (Textual) |
|--------|--------------------------|----------------------|
| **Base OS RAM** | ~250 MB | ~120 MB |
| **Franklin GUI/TUI** | ~286 MB | ~55 MB |
| **sway compositor** | ~90 MB | N/A |
| **wayvnc** | ~9 MB | N/A |
| **Total idle** | **~635 MB** | **~175 MB** |
| **Audio** | Full (sounddevice + aplay) | Terminal bell only |
| **Start lights** | Visual countdown | Status text only |

**Savings: ~460 MB RAM** — critical for Pi 3, Pi Zero 2 W, or Pi 4 with 2GB.

## Hardware Compatibility

| Pi Model | 64-bit GUI | 32-bit TUI |
|----------|------------|------------|
| Pi 5 (8GB/4GB) | ✅ Recommended | ✅ Works |
| Pi 4 (4GB/2GB) | ✅ Works | ✅ Recommended for 2GB |
| Pi 4 (1GB) | ❌ OOM risk | ✅ Works |
| Pi 3B+ | ❌ Too slow | ✅ Works |
| Pi Zero 2 W | ❌ No 64-bit OS | ✅ Primary target |

## Cross-Compilation (macOS/Linux → armhf)

The Rust hardware monitor must be cross-compiled for `armv7-unknown-linux-gnueabihf`.

### Prerequisites

```bash
# On macOS with devbox (includes cross, cargo, docker)
devbox shell

# Verify cross is available
cross --version
```

### Build .deb for 32-bit Pi

```bash
# Set target architecture
export RUST_PI_TARGET=armv7-unknown-linux-gnueabihf

# Build the .deb package
devbox run hardware-monitor:build-deb

# Output: rust/target/debian/franklin-hardware-monitor_<version>_armhf.deb
```

### Deploy to 32-bit Pi

```bash
# 1. Full machine setup (packages, users, systemd, etc.)
devbox run ansible:setup

# 2. Deploy artifacts (auto-detects armhf, uses correct .deb)
devbox run deploy

# 3. Start web apps (driver + referee)
devbox run ansible:web-bounce
```

## Architecture Detection

The deploy playbook (`deploy-franklin.yml`) auto-detects the Pi architecture:

```yaml
- name: Detect Pi architecture
  ansible.builtin.command: dpkg --print-architecture
  register: pi_arch_cmd

- name: Set Rust target and Debian arch label
  ansible.builtin.set_fact:
    rust_target: "{{ 'aarch64-unknown-linux-gnu' if detected_arch == 'arm64' else 'armv7-unknown-linux-gnueabihf' }}"
    arch_label: "{{ 'arm64' if detected_arch == 'arm64' else 'armhf' }}"
```

## Service Differences

### Enabled on 32-bit TUI
- `franklin-redis.service` — Redis Unix socket
- `franklin-hardware-monitor.service` — Serial → Redis bridge (Rust)
- `franklin-race-recorder.service` — Authoritative race model + DB
- `franklin-web-driver.service` — Racer view (port 8083)
- `franklin-web-referee.service` — Race control (port 8081)
- `franklin-tui.service` — Terminal UI (systemd, optional)

### Disabled on 32-bit TUI
- `franklin-web-scoreboard.service` — Spectator view
- `franklin-web-healthcheck.service` — Health monitoring
- `sway` / `wayvnc` — Wayland compositor + VNC
- `franklin-gui.py` — GTK4 GUI

## TUI Features vs GUI

| Feature | GUI (GTK4) | TUI (Textual) |
|---------|------------|---------------|
| Race modes (Real/Fake/Training) | ✅ | ✅ |
| Start race countdown | ✅ Visual + audio | ✅ Text + bell |
| Leaderboard | ✅ Table + colors | ✅ DataTable |
| Lap history | ✅ Scrollable log | ✅ Static panel |
| Driver management | ✅ Dialog | ✅ Modal screen |
| Penalties/DQ | ✅ Buttons | ✅ Commands via web |
| Race control (pause/end) | ✅ Buttons | ✅ Keybindings |
| Hardware status | ✅ Indicator | ✅ Header text |
| Sound (ready/set/go/finish) | ✅ sounddevice/aplay | ❌ Terminal bell only |
| Start light simulation | ✅ Visual widgets | ❌ N/A |

## TUI Keybindings

| Key | Action |
|-----|--------|
| `t` | Toggle race mode (Fake → Real → Training) |
| `s` | Start race (3-2-1 countdown) |
| `space` | Pause/Resume |
| `e` | End race |
| `r` | Rename driver |
| `q` / `Ctrl+c` | Quit |

## Boot Behavior

On 32-bit, the autologin `.zprofile` launches the TUI directly in tmux:

```bash
# ~/.zprofile (managed by Ansible)
if [[ -z "${SSH_TTY:-}" ]] && [[ -z "${TMUX:-}" ]] && [[ "$(tty)" == "/dev/tty1" ]]; then
  if command -v tmuxinator >/dev/null 2>&1; then
    cd "/opt/franklin-lap-counter"
    tmuxinator start franklin-tui 2>/dev/null || \
      tmux new-session -s franklin-tui -d 'python franklin-tui.py' && \
      tmux attach -t franklin-tui
    exit
  fi
fi
```

## Memory Optimization Notes

The 32-bit TUI deployment disables these memory-heavy components:
- **healthcheck_web_app.py** — was using ~90 MB RSS (possible leak)
- **scoreboard_web_app.py** — ~27 MB
- **sway** — ~90 MB
- **wayvnc** — ~9 MB
- **GTK4/PyGObject** — ~100+ MB in-process

## Troubleshooting

### "Could not verify armhf architecture in deployed binary"
The `file` command output on 32-bit shows `ARM` not `ARM 64-bit`. The playbook checks for generic `ARM` string on armhf.

### Hardware monitor fails to start
```bash
# Check binary architecture
file /usr/bin/franklin-hardware-monitor
# Should show: ELF 32-bit LSB executable, ARM, EABI5 version 1 (SYSV), dynamically linked

# Check logs
journalctl -u franklin-hardware-monitor -f
```

### TUI not starting on boot
```bash
# Check autologin
systemctl status getty@tty1.service

# Check .zprofile
cat /home/franklin/.zprofile

# Manual test
sudo -u franklin tmux new-session -s test -d 'cd /opt/franklin-lap-counter && .venv/bin/python franklin-tui.py'
```

## Building Locally on 32-bit Pi (Alternative)

If cross-compilation fails, build natively on the Pi:

```bash
# On the Pi (32-bit OS)
cd /opt/franklin-lap-counter/rust
cargo build --release --target armv7-unknown-linux-gnueabihf

# Then create .deb manually or use the deploy playbook with local binary already-built binary
```

## Reverting to 64-bit GUI

To switch back to full GUI mode:
1. Flash 64-bit Pi OS Lite
2. Re-enable in `group_vars/all.yml`:
   ```yaml
   franklin_enable_wayland_boot: true
   franklin_enable_wayvnc: true
   ```
3. Add back to `franklin.target`:
   ```yaml
   Wants=franklin-web-scoreboard.service franklin-web-healthcheck.service
   ```
4. Re-run `devbox run ansible:setup && devbox run deploy`