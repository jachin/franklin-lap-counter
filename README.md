# Franklin Lap Counter

This is lap counter software for [Trackmate Racing RC Lap Counter](https://trackmateracing.com/shop/en/r-c-lap-counter-transponder-system/122-759-rc-lap-counter.html#/126-software-free_download). It comes with free software but it's Windows only. This project aims to support other operating systems and hopefully allow for something that might fit more people's needs.

## The Name

The name is a nod to [Benjamin Franklin Miessner](https://en.wikipedia.org/wiki/Benjamin_Miessner) who was a radio engineer and inventor.

## Features

- ✅ **Redis pub/sub architecture** for flexible, decopuled component communication
- ✅ **Simulation mode** for fully-featured testing without physical hardware
- ✅ **Real-time leaderboard display** (TUI and GTK GUI options)
- ✅ **Driver/Team web view** with countdown lights and customized racer progress
- ✅ **Scoreboard web view** displaying live, updated race positions
- ✅ **Referee web interface** supporting start, end, reset, penalties, and lap edits
- ✅ **Automated heartbeats** every 2 seconds for connection health monitoring
- ✅ **Flexible contestant and roster management**
- ✅ **Comprehensive, multi-layer logging**

## Architecture

The system uses Redis for communication between components, allowing you to run the hardware interface, race UI, scoreboard web app, and referee web app in separate terminals.

```
┌─────────────────────┐         ┌─────────────┐         ┌──────────────────────┐
│   Franklin (TUI)    │         │   Redis     │         │ Hardware Comm        │
│   Race Management   │◄───────►│  Pub/Sub    │◄───────►│ (Serial Interface)   │
│                     │         │             │         │                      │
└─────────────────────┘         └─────────────┘         └──────────────────────┘
     Terminal 1                   Auto-started              Terminal 2
                                  by devbox
```

Alternative simplified view of data flows:

```
┌──────────────┐       ┌───────────────┐       ┌─────────────────┐
│  Your App    │       │     Redis     │       │ Hardware Comm   │
│  (Any Lang)  │◄─────►│   Pub/Sub     │◄─────►│  (This File)    │
└──────────────┘       └───────────────┘       └─────────────────┘
                             │
                             │
                       ┌─────┴─────┐
                       ▼           ▼
                 hardware:in  hardware:out
```

## Running the System

### Prerequisites
This project uses [Devbox](https://www.jetify.com/devbox) for environment management. Redis starts automatically when you enter the devbox shell.

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

# 2. Deploy all application artifacts and built binaries to target host
devbox run ansible:deploy

# 3. Ensure background web apps are up and running in tmux sessions
devbox run ansible:web-bounce

# 4. Run runtime health checks through the health-check web app
devbox run ansible:health-check
```

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
devbox run rust-hw-sim
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
devbox run rust-hw
```

**Terminal 2 - Race UI (Text TUI) or GTK GUI:**
```bash
devbox shell
python franklin-tui.py --race
# OR
python franklin-gui.py --race
```

#### 3. Running a Self-Contained Fake Race (completely standalone)

No hardware interface or Redis backend setup is needed - Franklin generates a fake race internally:

```bash
devbox shell
python franklin-tui.py --fake
# OR
python franklin-gui.py --fake
```

---

## Web Applications

### Scoreboard Web App
Starts `scoreboard_web_app.py` on port `8080`. Serves the live scoreboard UI and WebSocket/REST endpoints for race data.
```bash
devbox run web_scoreboard
```
- Local access: `http://127.0.0.1:8080`
- Network access: `http://<pi-ip>:8080`

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
- `scoreboard.frank` → `scoreboard_web_app.py` (`127.0.0.1:8080`)
- `referee.frank` → `referee_web_app.py` (`127.0.0.1:8081`)
- `healthcheck.frank` → `healthcheck_web_app.py` (`127.0.0.1:8082`)
- `racer.frank` → `driver_web_app.py` (`127.0.0.1:8083`)

---

## Controls

### Franklin Race TUI
- **Ctrl+S** - Start Race
- **Ctrl+X** - End Race
- **Ctrl+T** - Toggle Mode (Fake/Real/Training)
- You can also click the interactive "Start Race" and "End Race" buttons directly.

### Hardware Interface (Real Mode - Terminal 1)
- **Q** - Quit

### Hardware Interface (Simulator Mode - Terminal 1)
- **S** - Start race (sends reset commands)
- **P** - Stop/Pause race
- **1-4** - Simulate lap for racer 1-4
- **Q** - Quit

---

## Configuration

Edit `franklin.config.json` in the root directory to configure the race rules and roster:

```json
{
  "total_laps": 10,
  "contestants": [
    {"id": 1, "name": "Racer 1"},
    {"id": 2, "name": "Racer 2"},
    {"id": 3, "name": "Racer 3"},
    {"id": 4, "name": "Racer 4"}
  ]
}
```

---

## Available Devbox Scripts

Run these tasks using `devbox run <script-name>` inside your devbox environment:

**Core Ansible Workflow:**
- `ansible:setup` - Full machine setup (packages, services, AP routing, Caddy, etc.)
- `ansible:deploy` - Deploy full app artifacts and built binaries to target host
- `ansible:deploy-gui` - Deploy GUI-focused python/runtime files only (fast path)
- `deploy-gui` - Fast GUI deploy + web-app bounce (`ansible:deploy-gui` then `ansible:web-bounce`)
- `ansible:web-bounce` - Ensure all tmux web windows are running and active
- `ansible:health-check` - Run runtime health verification via the health check app
- `ansible:reboot` - Reboot target host via Ansible
- `ansible:hard-reset` - Completely wipe the target app directory and stop all running processes on the host

**Build / Local Execution:**
- `build` - Run all local build tasks
- `build:pi` - Build release hardware-monitor binary for the Pi target (`aarch64-unknown-linux-gnu`)
- `build:release` - Build local release binary of the Rust project
- `start:franklin` - Start full production Franklin tmux stack (hardware mode)
- `start:franklin-simulator` - Start simulator Franklin tmux stack (web apps auto-reload via `watchexec`)
- `web_scoreboard` - Run scoreboard web app locally
- `web_referee` - Run referee web app locally
- `web_healthcheck` - Run health-check web app locally

**Remote GUI (VNC over SSH Tunnel):**
- `vnc:open-tunnel` - Open local tunnel `127.0.0.1:5901 -> Pi:5900`
- `vnc:connect` - Launch default VNC viewer to connect via tunnel

**Quality Checks & Testing:**
- `lint` - Run all Python, Web, and Rust linter checks
- `lint:python` / `lint:web` / `lint:rust` - Run targeted language linter checks
- `test` - Run all Python and Rust automated test suites
- `test:python` / `test:rust` - Run targeted test suites

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
cargo run --manifest-path rust/Cargo.toml --bin franklin-hardware-monitor -- --sim

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

---

**Note:** For the best performance, timing accuracy, and minimal jitter under heavy loads, always use the compiled Rust hardware monitor (`franklin-hardware-monitor`).
