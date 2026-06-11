# Quick Start Guide - Redis Backend

## ✅ What's Working

The Rust `franklin-hardware-monitor` is now fully functional with:
- ✅ Redis pub/sub communication
- ✅ Simulation mode (no hardware needed)
- ✅ Hardware mode (for real device)
- ✅ Automatic heartbeats every 2 seconds
- ✅ Redis connection testing on startup
- ✅ Full TUI with race simulation controls
- ✅ Verbose mode to show RAW and HEARTBEAT messages

## Running the System

### Deploy + run on target host (recommended workflow)

```bash
devbox run ansible:setup
devbox run ansible:deploy
devbox run ansible:web-bounce
devbox run ansible:health-check
```

### Local/full startup

```bash
# Hardware mode (assumes hardware is connected)
devbox run start:franklin

# Simulator mode (no hardware required)
devbox run start:franklin-simulator
```

### Manual Redis testing

```bash
# Terminal 1: subscribe to messages
devbox shell
redis-cli -s ./redis.sock
> SUBSCRIBE hardware:out

# Terminal 2: run hardware monitor in sim mode
devbox shell
cargo run --manifest-path rust/Cargo.toml --bin franklin-hardware-monitor -- --sim

# Terminal 3: send commands
devbox shell
redis-cli -s ./redis.sock
> PUBLISH hardware:in '{"type":"command","command":"start_race"}'
> PUBLISH hardware:in '{"type":"command","command":"simulate_lap","racer_id":1,"sensor_id":1,"race_time":5.5}'
# Note: `race_time` here is only the simulator command input (relative seconds).
# The emitted `hardware:out` lap event uses epoch fields: `race_start_at`, `lap_at`, `recorded_at`.
```

## What You Can Build Next

Now that Redis is in the middle, you can:

1. **Build a new TUI/GUI** - Subscribe to `hardware:out` and display race data
2. **Add persistence** - Store lap data in Redis or a database
3. **Web interface** - Use Redis pub/sub with WebSockets
4. **Multiple clients** - Multiple UIs can watch the same race
5. **Recording/Replay** - Record all events and replay them later

## Architecture

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

## Message Reference

Redis channel/message contracts are maintained in one canonical location:

- `docs/redis-message-reference.md`

Use that document for all payload examples and publisher/subscriber mappings.

## Logs

All activity is logged to: `hardware_redis.log`

## Available Devbox Scripts

**Core Ansible workflow:**
- `ansible:setup` - Full machine setup (packages/services/network/caddy/etc.)
- `ansible:deploy` - Deploy full app artifacts to target host
- `ansible:deploy-gui` - Deploy GUI-focused runtime files only (fast path)
- `deploy-gui` - Fast GUI deploy + web bounce (`ansible:deploy-gui` then `ansible:web-bounce`)
- `ansible:web-bounce` - Ensure tmux web windows are created/running (`web_scoreboard`, `web_referee`, `web_healthcheck`, `web_driver`)
- `ansible:health-check` - Run runtime health check through the health-check web app
- `ansible:reboot` - Reboot target host via Ansible

**Build / start:**
- `build` - Run all build tasks
- `build:pi` - Build release binary for Pi target (`aarch64-unknown-linux-gnu` by default)
- `build:release` - Build Rust project (release)
- `start:franklin` - Start full Franklin tmux stack (hardware mode)
- `start:franklin-simulator` - Start full Franklin tmux stack (simulator mode, web apps auto-restart via `watchexec`)
- `web_scoreboard` - Run scoreboard web app locally (`scoreboard_web_app.py`)
- `web_referee` - Run referee web app locally (`referee_web_app.py`)
- `web_healthcheck` - Run health-check web app locally (`healthcheck_web_app.py`)

**Remote GUI (VNC over SSH tunnel):**
- `vnc:open-tunnel` - Open local tunnel `127.0.0.1:5901 -> Pi:5900`
- `vnc:connect` - Open VNC client to `vnc://127.0.0.1:5901`

**Quality checks:**
- `lint` - Run all lint checks
- `lint:python` / `lint:web` / `lint:rust` - Run targeted lint checks
- `test` - Run all tests
- `test:python` / `test:rust` - Run targeted test suites

## Next Steps for Your Refactor

1. ✅ **Done:** Hardware communication layer with Redis (Python + Rust)
2. ✅ **Done:** Franklin TUI uses Redis for communication
3. **Next:** Test with real hardware
4. **Then:** Add any additional features (persistence, web UI, etc.)

---

**Note:** Use the Rust hardware monitor (`franklin-hardware-monitor`) for best performance and reliability.
