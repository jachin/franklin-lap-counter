# Go Hardware Comm - TUI Guide

## What's Been Added

✅ **Interactive Terminal UI** using Bubbletea (modern Go TUI framework)
✅ **Real-time message display** with color coding
✅ **Keyboard controls** for simulation mode
✅ **Clean, styled interface** with lipgloss

## Running with TUI

### Option 1: Using the binary directly

```bash
# From the main lap-counter directory
./go-hardware-comm/hardware-comm --sim --tui

# Or with hardware
./go-hardware-comm/hardware-comm --tui
```

### Option 2: Using devbox scripts

```bash
# Simulation mode with TUI
devbox run go-hardware-sim-tui

# Hardware mode with TUI
devbox run go-hardware-tui
```

### Option 3: Build and run

```bash
cd go-hardware-comm
go build -o hardware-comm
cd ..
./go-hardware-comm/hardware-comm --sim --tui
```

## TUI Features

### Display Elements

**Header**
- Shows mode (SIMULATION or HARDWARE)
- Colored banner at top

**Instructions**
- In simulation mode: Shows all available commands
- In hardware mode: Shows quit command only

**Message Window**
- Real-time display of hardware events
- Color-coded by message type:
  - 🟢 **Green**: Lap events
  - 🔴 **Pink**: Heartbeats (♥)
  - 🔵 **Cyan**: Status messages
  - 🔴 **Red**: Errors
  - ⚫ **Gray**: Debug messages
  - **White**: Raw data

**Status Bar**
- Message counter at bottom

### Keyboard Controls

#### Simulation Mode Only:
- **S** - Start race
- **P** - Stop race (stoP)
- **1** - Simulate lap for racer 1
- **2** - Simulate lap for racer 2
- **3** - Simulate lap for racer 3
- **4** - Simulate lap for racer 4

#### All Modes:
- **Q** - Quit
- **Ctrl+C** - Force quit

## How It Works

The TUI runs in the same process as the hardware communication but:

1. **Hardware comm runs in background goroutine**
   - Handles serial port communication
   - Manages Redis pub/sub
   - Sends messages to Redis

2. **TUI runs in foreground**
   - Subscribes to Redis output channel
   - Displays messages in real-time
   - Sends commands via Redis input channel

3. **Communication via Redis**
   - TUI → `hardware:in` → Hardware Comm → Serial Port
   - Serial Port → Hardware Comm → `hardware:out` → TUI

## Message Types

### LAP
```
[LAP] Racer 1 - Sensor 1 - Time: 45.123s
```
Shows when a racer completes a lap with timing.

### HEARTBEAT
```
[HEARTBEAT] ♥
```
Confirms hardware is responding (every 2 seconds in simulation).

### STATUS
```
[STATUS] Hardware connected and initialized
```
General status updates from the system.

### ERROR
```
[ERROR] Lap tracking hardware not found
```
Error messages (shown in red).

### DEBUG
```
[DEBUG] Read line: ...
```
Low-level debug information (shown in gray).

### RAW
```
[RAW] <raw serial data>
```
Unprocessed data from serial port.

## Comparison: Python TUI vs Go TUI

| Feature | Python (curses) | Go (bubbletea) |
|---------|----------------|----------------|
| Framework | ncurses/curses | Bubbletea |
| Styling | Basic colors | Rich styling with lipgloss |
| Performance | Good | Excellent |
| Code clarity | Imperative | Declarative (Elm architecture) |
| Screen updates | Manual | Automatic |
| State management | Manual | Built-in |
| Key handling | Manual polling | Event-driven |

## Troubleshooting

### TUI doesn't start
- Make sure Redis is running: `ls -la redis.sock`
- Check logs: `tail -f hardware_redis.log`
- Run without TUI to test: `./go-hardware-comm/hardware-comm --sim`

### No messages appearing
- Press **S** to start race (simulation mode only)
- Check that hardware comm started: `tail hardware_redis.log`
- Verify Redis connection in logs

### Screen corruption
- Try resizing terminal
- Press **Ctrl+C** and restart
- Make sure terminal supports ANSI colors

### Keys not working
- Make sure terminal has focus
- In hardware mode, only **Q** works
- Simulation mode has all keys

## Architecture

```
┌─────────────────────────────────────────┐
│            Go Process                    │
│                                          │
│  ┌────────────────┐  ┌───────────────┐  │
│  │  Hardware Comm │  │      TUI      │  │
│  │   (goroutine)  │  │  (main thread)│  │
│  └────────────────┘  └───────────────┘  │
│         │                    │           │
└─────────┼────────────────────┼───────────┘
          │                    │
          ├──> Redis ──> hardware:out ──┘
          │      ↑
          └──────┴─── hardware:in ←──────
                      (commands from TUI)
```

## Benefits of Go TUI

1. **Single binary** - No Python + curses dependencies
2. **Better performance** - Native compiled, efficient rendering
3. **Modern architecture** - Elm-style (Model-View-Update)
4. **Type safety** - Compile-time checked, no runtime surprises
5. **Better styling** - Rich colors and formatting with lipgloss
6. **Easier to maintain** - Declarative code, clear state management

## Next Steps

Try it out:

```bash
# Start the TUI
devbox run go-hardware-sim-tui

# Then:
# 1. Press 'S' to start race
# 2. Press '1', '2', '3', or '4' to simulate laps
# 3. Watch the messages appear in real-time
# 4. Press 'P' to stop race
# 5. Press 'Q' to quit
```

Enjoy your modern, fast, beautiful terminal UI! 🎨
