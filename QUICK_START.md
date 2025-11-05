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

### Option 1: Interactive TUI (Recommended for Testing)

**Python Version:**
```bash
# Start devbox shell (auto-starts Redis)
devbox shell

# Run in simulation mode
devbox run hw-sim
```

**Rust Version (Recommended):**
```bash
# Run in simulation mode
devbox run rust-hw-sim

# Run in hardware mode (connects to real device)
devbox run rust-hw

# Run with verbose mode (shows RAW and HEARTBEAT messages)
devbox run rust-hw-verbose
devbox run rust-hw-sim-verbose
```

**TUI Controls:**
- **S** - Start race (simulation mode only)
- **P** - Stop race (simulation mode only)
- **1, 2, 3, 4** - Simulate lap for racer 1-4 (simulation mode only)
- **Q** - Quit

**Command-line Flags:**
- `--sim` or `-s` - Run in simulation mode
- `--verbose` or `-v` - Show RAW and HEARTBEAT messages
- `--serial-port <path>` or `-p <path>` - Specify serial port path
- `--baudrate <rate>` or `-b <rate>` - Set baudrate (default: 9600)
- `--redis-socket <path>` - Specify Redis socket path

### Option 2: Automated Test

```bash
devbox run hw-test
```

This runs automated tests and shows you all the messages flowing through Redis.

**Note:** If you see duplicate lap events (6 instead of 3), you have another instance running. Kill it with:
```bash
pkill -f 'franklin-hardware-monitor'
```
Then run the test again.

### Option 3: Manual Redis Testing

```bash
# Terminal 1: Subscribe to messages
devbox shell
redis-cli -s ./redis.sock
> SUBSCRIBE hardware:out

# Terminal 2: Run hardware comm
devbox shell  
devbox run rust-hw-sim

# Terminal 3: Send commands
devbox shell
redis-cli -s ./redis.sock
> PUBLISH hardware:in '{"type":"command","command":"start_race"}'
> PUBLISH hardware:in '{"type":"command","command":"simulate_lap","racer_id":1,"sensor_id":1,"race_time":5.5}'
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

### Commands (Publish to `hardware:in`)

Start race:
```json
{"type": "command", "command": "start_race"}
```

Stop race:
```json
{"type": "command", "command": "stop_race"}
```

Simulate lap (simulation mode only):
```json
{
  "type": "command",
  "command": "simulate_lap",
  "racer_id": 1,
  "sensor_id": 1,
  "race_time": 12.345
}
```

### Events (Subscribe to `hardware:out`)

Heartbeat (every 2 seconds):
```json
{"type": "heartbeat"}
```

Status message:
```json
{"type": "status", "message": "Redis connected"}
```

Lap detected:
```json
{
  "type": "lap",
  "racer_id": 1,
  "sensor_id": 1,
  "race_time": 12.345
}
```

## Logs

All activity is logged to: `hardware_redis.log`

## Available Devbox Scripts

**Rust Hardware Communication:**
- `rust-hw` - Run hardware mode (connects to real device)
- `rust-hw-verbose` - Run hardware mode with verbose output
- `rust-hw-sim` - Run simulation mode
- `rust-hw-sim-verbose` - Run simulation mode with verbose output
- `rust-build` - Build the Rust project
- `rust-build-release` - Build optimized release version
- `rust-check` - Check Rust code for errors
- `rust-test` - Run Rust tests

**Python Hardware Communication:**
- `hardware-monitor` - Run Python hardware mode
- `hw-sim` - Run Python simulation mode

**Other Services:**
- `franklin` - Run the Franklin TUI for race management
- `web` - Run the web server

## Next Steps for Your Refactor

1. ✅ **Done:** Hardware communication layer with Redis (Python + Rust)
2. ✅ **Done:** Franklin TUI uses Redis for communication
3. **Next:** Test with real hardware
4. **Then:** Add any additional features (persistence, web UI, etc.)

---

**Note:** Use the Rust version (`rust-hw`) as it's more performant and has better error handling. The `--verbose` flag is useful for debugging but can clutter the display with HEARTBEAT and RAW messages during normal operation.
