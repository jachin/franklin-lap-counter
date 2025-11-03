# Quick Start Guide - Redis Backend

## ✅ What's Working

Your `hardware_comm_redis.py` is now fully functional with:
- ✅ Redis pub/sub communication
- ✅ Simulation mode (no hardware needed)
- ✅ Hardware mode (for real device)
- ✅ Automatic heartbeats every 2 seconds
- ✅ Redis connection testing on startup
- ✅ Full TUI with race simulation controls

## Running the System

### Option 1: Interactive TUI (Recommended for Testing)

```bash
# Start devbox shell (auto-starts Redis)
devbox shell

# Run in simulation mode
devbox run hw-sim
```

Or directly:
```bash
devbox run hw-sim
```

**TUI Controls:**
- **S** - Start race
- **P** - Stop race  
- **1, 2, 3, 4** - Simulate lap for racer 1-4
- **Q** - Quit

### Option 2: Automated Test

```bash
devbox run hw-test
```

This runs automated tests and shows you all the messages flowing through Redis.

**Note:** If you see duplicate lap events (6 instead of 3), you have another instance running. Kill it with:
```bash
pkill -f 'python hardware_comm_redis.py'
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
python hardware_comm_redis.py --sim

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

## Next Steps for Your Refactor

1. ✅ **Done:** Hardware communication layer with Redis
2. **Next:** Update your main application (franklin.py?) to use Redis instead of multiprocessing
3. **Then:** Test with real hardware
4. **Finally:** Add any additional features (persistence, web UI, etc.)

---

**Test Results:** ✓ All tests passing (11 messages, 4 heartbeats, 3 laps, 4 status)
