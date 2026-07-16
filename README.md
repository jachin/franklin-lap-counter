# Franklin Lap Counter

This is lap counter software for [Trackmate Racing RC Lap Counter](https://trackmateracing.com/shop/en/r-c-lap-counter-transponder-system/122-759-rc-lap-counter.html#/126-software-free_download). It comes with free software but it's Windows only. This project aims to support other operating systems and hopefully allow for something that might fit more people's needs.

## The Big Idea

You need
 - [Trackmate Racing RC Lap Counter](https://trackmateracing.com/shop/en/10-r-c-lap-counter-transponder-system)
 - [Raspberry PI](https://www.raspberrypi.com)
   Probably just about any model will work) but so far I've tested it on a PI5.

 After installing RasberryPI OS you run the Ansible playbooks included in this project and it setups the PI as a race tracking system.

 **Note:** Ethernet must be connected during initial install for package downloads and system configuration. If you do not need the hotspot/router stack, set `franklin_enable_hotspot: false` in `playbooks/group_vars/all.yml`.

## Features

- Race Mode
- Training Mode

### Kiosk Mode

When the PI boots up it auto logins and starts up the Franklin Lap Counter.

- TUI Interface (default on 32-bit, optional on 64-bit)
- GUI Interface (GTK, 64-bit only)

### 32-bit Raspberry Pi OS Support (TUI-only)

Franklin supports **Raspberry Pi OS Lite 32-bit (armhf)** on Pi 3, Pi 4, Pi Zero 2 W, and Pi 5. The 32-bit build uses the Textual TUI instead of the GTK4 GUI, providing significant memory savings (~400 MB less RAM).

| Component | 64-bit (GUI) | 32-bit (TUI) |
|-----------|--------------|--------------|
| OS Base | Pi OS Lite 64-bit | Pi OS Lite 32-bit |
| Display | Wayland/sway + GTK4 | Terminal (Textual) |
| Memory (idle) | ~650 MB | ~240 MB |
| Audio | sounddevice/aplay | Terminal bell only |
| Start lights | Visual (GUI) | Status bar only |

**To deploy on 32-bit Pi OS:**
1. Flash Raspberry Pi OS Lite (32-bit) to SD card
2. Configure `playbooks/inventory.ini` with your Pi's IP
3. Run `devbox run ansible:setup`
4. Run `devbox run deploy:tui` (console/TUI mode) or `devbox run deploy:gui`
   (GTK GUI under sway/Wayland). The playbook auto-detects `armhf` and
   builds/installs the correct `.deb`.
5. Each task both builds the hardware-monitor `.deb` and deploys all artifacts,
   and toggles the console vs. GUI mode (enabling/disabling `franklin-tui`,
   `franklin-gui`, and `franklin-sway` services) so no extra commands are needed
   to switch between modes.

See `README-32bit-TUI.md` for detailed comparison and cross-compilation notes.

### Web Interface

## Running the System

### Prerequisites
- **Rust toolchain** — The hardware monitor is written in Rust. `rustup` is already in devbox, so no separate install is needed. Inside a devbox/tmux session, set the default toolchain:
  ```bash
  rustup default stable
  ```
- **Devbox** — This project uses [Devbox](https://www.jetify.com/devbox) for environment management. Redis starts automatically when you enter the devbox shell.

```bash
# Enter the development environment
devbox shell
```

---

### Method A: Deploy + Run on Target Host (Recommended Workflow)

If you are deploying to a target Raspberry Pi host over the network, use the following core Ansible tasks:

```bash
# 1. Perform full machine setup (packages, services, AP configuration, Caddy, etc.)
devbox run ansible:setup

# 2. Build the hardware monitor .deb and deploy all artifacts, choosing the UI mode:
#    - TUI/console mode (franklin-tui.service on tty1):
devbox run deploy:tui
#    - GTK GUI mode under sway/Wayland (franklin-gui + franklin-sway):
devbox run deploy:gui
#    Each task also enables/disables the opposite mode's services, so switching
#    modes is a single command with no extra steps.

# 3. Ensure background web apps are up and running in tmux sessions
devbox run ansible:web-bounce

# 4. Run runtime health checks through the health-check web app
devbox run ansible:health-check
```

> **Note:** The old `devbox run deploy` task has been removed. It deployed in
> TUI mode by default (via `ansible:deploy`, which leaves `franklin_gui_enabled`
> unset → `false`) but did **not** run the sway playbook, so it could not enable
> GUI mode. Use `deploy:tui` or `deploy:gui` explicitly instead. For a TUI-only
> artifact push without rebuilding the hardware monitor, `devbox run
> ansible:deploy` is still available.

---

### Updating Franklin on a Pi

For routine updates (new version, bug fixes), use the single-command update workflow. It auto-detects your Pi's architecture, builds the correct binary, and deploys everything in one step:

```bash
# Update one Pi (architecture is auto-detected via SSH)
devbox run update:franklin
```

This playbook does the following per host:
1. Detects the Pi's architecture (`dpkg --print-architecture`)
2. Builds `franklin-hardware-monitor` for that arch locally
3. Installs the `.deb` package on the Pi
4. Rsyncs Python source files (same approach as `deploy-franklin.yml`)
5. Updates Franklin's pip dependencies (`textual`, `redis`, `aiohttp`, `pygments`, `rich`)
6. Restarts all Franklin services via `franklin.target`

If you have multiple Pies in your inventory, the playbook processes each host independently — a build failure on one does not stop updates to others.

#### Inventory setup

Copy the example and edit it with your Pi's details:

```bash
cp playbooks/inventory.example.ini playbooks/inventory.ini
# Edit playbooks/inventory.ini with your Pi hostname/IP
```

Example `playbooks/inventory.ini`:

```ini
[pi]
franklin-pi ansible_user=franklin ansible_host=10.27.1.64
```

**Note:** The `franklin` user is automatically added to the `sudo` group during setup (`playbooks/20-python-venv.yml`), so it has sudo access for managing services and packages.

---

### Method B: Full tmux Stack Startup (Local or Remote)

You can launch the entire system (including background services) inside a pre-configured tmux session using `tmuxinator`.

```bash
# Hardware Mode (assumes real hardware is connected)
devbox run start:franklin

# Simulator Mode (no hardware required; web apps auto-restart via watchexec)
devbox run start:franklin-simulator
```

---

### Method C: Terminal-by-Terminal Manual Startup (Local)

If you prefer to start components individually in separate terminals:

#### 1. Running in Simulation Mode (no hardware needed)

**Terminal 1 - Hardware Simulator:**
```bash
devbox shell
devbox run hardware-monitor:run -- --sim
```

**Terminal 2 - Race UI (Text TUI) or GTK GUI:**
```bash
devbox shell
python franklin-tui.py --race
# OR
python franklin-gui.py --race
```

#### 2. Running a Real Race (with physical hardware connected)

**Terminal 1 - Hardware Interface:**
```bash
devbox shell
devbox run hardware-monitor:run
```

**Terminal 2 - Race UI (Text TUI) or GTK GUI:**
```bash
devbox shell
python franklin-tui.py --race
# OR
python franklin-gui.py --race
```

#### 3. Running a Fake Race (no hardware required)

A fake race generates synthetic laps so you can test the UI without physical hardware. The race recorder must be running — it owns the race model and generates the fake laps. The TUI/GUI are pure renderers that subscribe to the recorder's state.

**Terminal 1 — Headless Recorder (required):**
```bash
devbox shell
python franklin-race-recorder.py
```

**Terminal 2 — TUI or GUI renderer:**
```bash
devbox shell
python franklin-tui.py --fake
# OR
python franklin-gui.py --fake
```

You can also launch the full stack (redis + hardware simulator + recorder + web apps + TUI) in a single tmux session for local development:
```bash
devbox run start:franklin-simulator
```

---

## Web Applications

### Scoreboard Web App
Starts `scoreboard_web_app.py` on port `8085`. Serves the live scoreboard UI and WebSocket/REST endpoints for race data.
```bash
devbox run web_scoreboard
```
- Local access: `http://127.0.0.1:8085`

- Network access: `http://<pi-ip>:8085`
### Driver Web App
Starts `driver_web_app.py` on port `8083`. Enables drivers or teams to view real-time countdown lights, specific racer details (position, progress, best/last lap, penalties), and practice/training mode lap histories.
```bash
devbox run web_driver
```
- Local access: `http://127.0.0.1:8083`
- Network access: `http://<pi-ip>:8083`

### Referee Web App
Starts `referee_web_app.py` on port `8081`. Allows race controllers to trigger starts, ends, resets, add penalties, remove invalid laps, or disqualify contestants. Action logs are audit-logged to SQLite.
```bash
devbox run web_referee
```
- Local access: `http://127.0.0.1:8081`
- Network access: `http://<pi-ip>:8081`
- Design specifications: See `docs/referee-web-app-design.md`

### Local Hostnames (Hotspot AP + Caddy Proxy)
When the Raspberry Pi hotspot and Caddy reverse proxy are active, the following local domains route automatically:
- `scoreboard.frank` → `scoreboard_web_app.py` (`127.0.0.1:8085`)
- `referee.frank` → `referee_web_app.py` (`127.0.0.1:8081`)
- `healthcheck.frank` → `healthcheck_web_app.py` (`127.0.0.1:8082`)
- `racer.frank` → `driver_web_app.py` (`127.0.0.1:8083`)

---

## Testing

### Automated Tests
Run the combined test suite:
```bash
devbox run test
```

### Manual Redis Testing
You can inspect pub/sub messages manually to test the hardware layer interfaces:

```bash
# Terminal 1: Subscribe to lap/state output events
devbox shell
redis-cli -s ./redis.sock
> SUBSCRIBE hardware:out

# Terminal 2: Run hardware monitor in simulator mode
devbox shell
devbox run hardware-monitor:run -- --sim

# Terminal 3: Publish mock commands
devbox shell
redis-cli -s ./redis.sock
> PUBLISH hardware:in '{"type":"command","command":"start_race"}'
> PUBLISH hardware:in '{"type":"command","command":"simulate_lap","racer_id":1,"sensor_id":1,"race_time":5.5}'
```

---

## Logs

- `race.log` - Franklin race control events and rule-checking decisions
- `hardware_redis.log` - Serial communications and Redis pub/sub bridges
- `gui.log` - GTK GUI logs and engine status
- `redis.log` - Redis daemon activity log
- `web.log` - Output logs for the Scoreboard, Referee, and Driver servers

---

## Redis Communication Reference

The canonical Redis channel/message schemas and publishers/subscribers mapping are maintained in one authoritative document:

- `docs/redis-message-reference.md`

Use that document as the reference source when adding components or refactoring event contracts.

---

## Troubleshooting

### Redis connection issues
If you receive "Failed to connect to Redis", make sure you are working inside the devbox shell:
```bash
devbox shell
```
The Redis server daemonizes automatically on environment initialization.

### Hardware not detected
If Franklin displays "Lap counter not detected":
1. Verify the `franklin-hardware-monitor` binary is running
2. Confirm the 2-second heartbeat logs are emitting in `hardware_redis.log`
3. Ensure serial cables are connected properly. The default fallback serial interface paths are:
   - macOS: `/dev/tty.usbserial-AB0KLIK2`
   - Linux: `/dev/ttyUSB0`

### Duplicate lap events
If multiple laps are triggered near-instantaneously, duplicate interface agents might be active:
```bash
pkill -f 'franklin-hardware-monitor'
pkill -f 'python franklin-tui.py'
```
Then restart your preferred stack.


## The Name

The name is a nod to [Benjamin Franklin Miessner](https://en.wikipedia.org/wiki/Benjamin_Miessner) who was a radio engineer and inventor.
