# Franklin Lap Counter

This is lap counter software for [Trackmate Racing RC Lap Counter](https://trackmateracing.com/shop/en/r-c-lap-counter-transponder-system/122-759-rc-lap-counter.html#/126-software-free_download). It comes with free software but it's Windows only. This project aims to support other operating systems and hopefully allow for something that might fit more people's needs.

## The Big Idea

You need
 - [Trackmate Racing RC Lap Counter](https://trackmateracing.com/shop/en/10-r-c-lap-counter-transponder-system)
 - [Raspberry PI](https://www.raspberrypi.com)
   Probably just about any model will work) but so far I've tested it on a PI5.

 After installing RasberryPI OS you run the Ansible playbooks included in this project and it setups the PI as a race tracking system.

## Features

- Race Mode
- Training Mode

### Kisok Mode

Whe the PI boots up it auto logsin and starts up the Franklin Lap Couter.

- TUI Interface
- GUI Interface (GTK)

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

You can also launch the full stack (recorder + web apps + renderer) in a single tmux session:
```bash
devbox run start:franklin-simulator
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


## The Name

The name is a nod to [Benjamin Franklin Miessner](https://en.wikipedia.org/wiki/Benjamin_Miessner) who was a radio engineer and inventor.
