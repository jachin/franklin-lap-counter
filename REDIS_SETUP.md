# Redis Backend Setup

## Quick Start

Redis is automatically started when you enter the devbox shell.

### 1. Run Hardware Communication in Simulation Mode
```bash
devbox run hw-sim
```

### 2. Run Automated Tests
```bash
devbox run hw-test
```

### 3. Run with Real Hardware
```bash
devbox shell
python hardware_comm_redis.py
```

Or use the short form for simulation:
```bash
python hardware_comm_redis.py --sim
# or
python hardware_comm_redis.py -s
```

## TUI Controls

### Simulation Mode
- **S** - Start race (begins tracking race time)
- **P** - Stop race
- **1** - Simulate lap event for racer 1
- **2** - Simulate lap event for racer 2
- **3** - Simulate lap event for racer 3
- **4** - Simulate lap event for racer 4
- **Q** - Quit

### Hardware Mode
- **S** - Start race (sends reset commands to hardware)
- **P** - Stop race
- **Q** - Quit

## Redis Channels

### `hardware:in`
Commands sent TO the hardware process:
```json
{"type": "command", "command": "start_race"}
{"type": "command", "command": "stop_race"}
{"type": "command", "command": "simulate_lap", "racer_id": 1, "sensor_id": 1, "race_time": 12.345}
```

### `hardware:out`
Messages FROM the hardware process:
```json
{"type": "heartbeat"}
{"type": "status", "message": "..."}
{"type": "lap", "racer_id": 1, "sensor_id": 1, "race_time": 12.345}
{"type": "debug", "message": "..."}
{"type": "raw", "line": "..."}
```

## Testing Redis Connection

You can test Redis manually:
```bash
redis-cli -s ./redis.sock ping
```

Subscribe to messages:
```bash
redis-cli -s ./redis.sock
> SUBSCRIBE hardware:out
```

Publish a command:
```bash
redis-cli -s ./redis.sock
> PUBLISH hardware:in '{"type":"command","command":"start_race"}'
```

## Architecture

```
┌─────────────────┐         ┌─────────────┐         ┌─────────────────┐
│   GUI/TUI       │         │   Redis     │         │ Hardware Comm   │
│   (Frontend)    │◄───────►│  Pub/Sub    │◄───────►│   Process       │
│                 │         │             │         │                 │
└─────────────────┘         └─────────────┘         └─────────────────┘
     Publishes to               Channels:                Publishes to
     hardware:in                - hardware:in            hardware:out
                                - hardware:out
```

## Features

- ✅ Redis pub/sub communication
- ✅ Simulation mode (no hardware required)
- ✅ Hardware mode (connects to serial device)
- ✅ Automatic heartbeat monitoring
- ✅ Redis connection testing on startup
- ✅ Comprehensive logging to `hardware_redis.log`
