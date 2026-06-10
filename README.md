# Franklin Lap Counter

This is lap counter software for [Trackmate Racing RC Lap Counter](https://trackmateracing.com/shop/en/r-c-lap-counter-transponder-system/122-759-rc-lap-counter.html#/126-software-free_download). It comes with free software but it's Windows only. This project aims to support other operating systems and hopefully allow for something that might fit more people's needs.

## The Name

The name is a nod to [Benjamin Franklin Miessner](https://en.wikipedia.org/wiki/Benjamin_Miessner) who was a radio engineer and inventor.

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
devbox run rust-hw
```

**Terminal 2 - Race UI (Text UI):**
```bash
devbox shell
python franklin-tui.py --race
```

**Terminal 2 - Race UI (GTK GUI, optional):**
```bash
devbox shell
python franklin-gui.py --race
```

### Running in Simulation Mode (no hardware needed)

**Terminal 1 - Hardware Simulator:**
```bash
devbox shell
devbox run rust-hw-sim
```

**Terminal 2 - Race UI (Text UI):**
```bash
devbox shell
python franklin-tui.py --race
```

**Terminal 2 - Race UI (GTK GUI, optional):**
```bash
devbox shell
python franklin-gui.py --race
```

### Running a Fake Race (self-contained)

No hardware interface needed - Franklin generates a fake race internally:
```bash
devbox shell
python franklin-tui.py --fake
```

Or with GTK GUI:
```bash
devbox shell
python franklin-gui.py --fake
```

## Scoreboard Web App

Run the scoreboard web app:

```bash
devbox shell
devbox run web_scoreboard
```

This starts `scoreboard_web_app.py` on `0.0.0.0:8080`.

- On the Pi itself: `http://127.0.0.1:8080`
- From a device on the same network or AP: `http://<pi-ip>:8080`

The app serves the live scoreboard UI and exposes WebSocket/REST endpoints used for race data.

## Driver Web App

Run the driver/team web app:

```bash
devbox shell
devbox run web_driver
```

This starts `driver_web_app.py` on `0.0.0.0:8083`.

- On the Pi itself: `http://127.0.0.1:8083`
- From a device on the same network or AP: `http://<pi-ip>:8083`

Current behavior:

- lets a driver/team pick a racer from the roster
- shows racer name + 4 start lights matching GUI countdown/race state
- in practice mode (`Training Mode`), shows the last 10 laps and highlights the fastest lap
- in race mode, shows racer-specific detail (position, lap progress, best/last lap, elapsed/adjusted totals, penalties, and gap to leader)

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

Edit `franklin.config.json` to configure your race:

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

The canonical Redis protocol reference lives in:

- `docs/redis-message-reference.md`

Use that document for:

- all channel definitions
- all message schemas
- publisher/subscriber ownership
- known contract inconsistencies

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
1. Check that `franklin-hardware-monitor` is running in Terminal 1
2. Look for heartbeat messages in Terminal 1's output
3. Verify the hardware is connected to the correct serial port
4. Check `hardware_redis.log` for errors

### Duplicate lap events
If you see duplicate lap events, you may have multiple instances running:
```bash
pkill -f 'franklin-hardware-monitor'
pkill -f 'python franklin-tui.py'
```
Then restart both processes.

### Serial port errors
The default serial port is:
- macOS: `/dev/tty.usbserial-AB0KLIK2`
- Linux: `/dev/ttyUSB0`

You can modify `franklin-hardware-monitor` to use a different port if needed.

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

## Referee Web App

Run the referee web app:

```bash
devbox shell
devbox run web_referee
```

This starts `referee_web_app.py` on `0.0.0.0:8081`.

- On the Pi itself: `http://127.0.0.1:8081`
- From a device on the same network/AP: `http://<pi-ip>:8081`

Current supported referee actions:

- Start race (`start_race`)
- End race (`end_race`)
- Reset race (`reset_race`)
- Remove lap (`remove_lap`, specific lap or latest lap for racer)
- Add penalty (`add_penalty`, 5-second increments)
- Disqualify racer (`disqualify_racer`)

Design notes and architecture:

- `docs/referee-web-app-design.md`
- `docs/redis-message-reference.md` (authoritative Redis contract)

Audit logging:

- Race-control outcomes are persisted to SQLite table `race_control_actions` in `lap_counter.db`.

## Features

- ✅ Redis pub/sub architecture for flexible component communication
- ✅ Simulation mode for testing without hardware
- ✅ Real-time leaderboard display
- ✅ Driver/team-focused racer web view
- ✅ Lap time tracking and analysis
- ✅ Multiple race modes (Real, Fake, Training)
- ✅ Contestant management
- ✅ Automatic hardware initialization
- ✅ Comprehensive logging
- ✅ TUI with Textual framework
