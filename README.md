# Franklin Lap Counter

This is lap counter software for [Trackmate Racing RC Lap Counter](https://trackmateracing.com/shop/en/r-c-lap-counter-transponder-system/122-759-rc-lap-counter.html#/126-software-free_download). It comes with free software but it's Windows only. This project aims to support other operating systems and hopefully allow for something that might fit more people's needs.

## The Name

The name is a nod to [Benjamin Franklin Miessner](https://en.wikipedia.org/wiki/Benjamin_Miessner) who was a radio engineer and inventor.

## Architecture

The system uses Redis for communication between components, allowing you to run the hardware interface and race UI in separate terminals.

```
┌─────────────────────┐         ┌─────────────┐         ┌──────────────────────┐
│   Franklin (TUI)    │         │   Redis     │         │ Hardware Comm        │
│   Race Management   │◄───────►│  Pub/Sub    │◄───────►│ (Serial Interface)   │
│                     │         │             │         │                      │
└─────────────────────┘         └─────────────┘         └──────────────────────┘
     Terminal 1                   Auto-started              Terminal 2
                                  by devbox
```

## Quick Start

### Prerequisites
This project uses [Devbox](https://www.jetify.com/devbox) for environment management. Redis starts automatically when you enter the devbox shell.

```bash
# Enter the development environment
devbox shell
```

### Running a Real Race (with hardware)

**Terminal 1 - Hardware Interface:**
```bash
devbox shell
python hardware_comm_redis.py
```

**Terminal 2 - Race UI:**
```bash
devbox shell
python franklin.py --race
```

### Running in Simulation Mode (no hardware needed)

**Terminal 1 - Hardware Simulator:**
```bash
devbox shell
python hardware_comm_redis.py --sim
```

**Terminal 2 - Race UI:**
```bash
devbox shell
python franklin.py --race
```

### Running a Fake Race (self-contained)

No hardware interface needed - Franklin generates a fake race internally:
```bash
devbox shell
python franklin.py --fake
```

## Controls

### Franklin Race UI (Terminal 2)
- **Ctrl+S** - Start Race
- **Ctrl+X** - End Race
- **Ctrl+T** - Toggle Mode (Fake/Real/Training)
- Click "Start Race" / "End Race" buttons in the TUI

### Hardware Interface - Real Hardware Mode (Terminal 1)
- **Q** - Quit

### Hardware Interface - Simulation Mode (Terminal 1)
- **S** - Start race (sends reset commands)
- **P** - Stop race
- **1-4** - Simulate lap for racer 1-4
- **Q** - Quit

## Configuration

Edit `config.json` to configure your race:

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

## Redis Communication

The system uses two Redis pub/sub channels for inter-process communication:

### `hardware:in` - Commands TO Hardware
```json
{"type": "command", "command": "start_race"}
{"type": "command", "command": "stop_race"}
{"type": "command", "command": "simulate_lap", "racer_id": 1, "sensor_id": 1, "race_time": 12.5}
```

### `hardware:out` - Events FROM Hardware
```json
{"type": "heartbeat"}
{"type": "status", "message": "Hardware connected"}
{"type": "lap", "racer_id": 1, "sensor_id": 1, "race_time": 12.345}
{"type": "debug", "message": "..."}
{"type": "raw", "line": "..."}
```

## Testing

### Automated Tests
```bash
devbox run hw-test
```

### Manual Redis Testing
```bash
# Check Redis is running
redis-cli -s ./redis.sock ping

# Monitor all messages
redis-cli -s ./redis.sock
> SUBSCRIBE hardware:out

# Send test command
redis-cli -s ./redis.sock
> PUBLISH hardware:in '{"type":"command","command":"start_race"}'
```

## Troubleshooting

### Redis not running
If you see "Failed to connect to Redis", make sure you're in a devbox shell:
```bash
devbox shell
```
Redis starts automatically when you enter the devbox shell.

### Hardware not detected
If Franklin shows "Lap counter not detected":
1. Check that `hardware_comm_redis.py` is running in Terminal 1
2. Look for heartbeat messages in Terminal 1's output
3. Verify the hardware is connected to the correct serial port
4. Check `hardware_redis.log` for errors

### Duplicate lap events
If you see duplicate lap events, you may have multiple instances running:
```bash
pkill -f 'python hardware_comm_redis.py'
pkill -f 'python franklin.py'
```
Then restart both processes.

### Serial port errors
The default serial port is:
- macOS: `/dev/tty.usbserial-AB0KLIK2`
- Linux: `/dev/ttyUSB0`

You can modify `hardware_comm_redis.py` to use a different port if needed.

## Logs

- `race.log` - Franklin race events and decisions
- `hardware_redis.log` - Hardware communication and serial data

## Development

### Running Tests
```bash
devbox shell
pytest
```

### Type Checking
```bash
devbox shell
basedpyright
```

## Features

- ✅ Redis pub/sub architecture for flexible component communication
- ✅ Simulation mode for testing without hardware
- ✅ Real-time leaderboard display
- ✅ Lap time tracking and analysis
- ✅ Multiple race modes (Real, Fake, Training)
- ✅ Contestant management
- ✅ Automatic hardware initialization
- ✅ Comprehensive logging
- ✅ TUI with Textual framework
