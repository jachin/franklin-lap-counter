# Redis Message & Channel Reference (Canonical)

This is the **single source of truth** for Redis pub/sub channels and message contracts in Franklin Lap Counter.

If another document or code comment conflicts with this one, treat this file as authoritative and update the other location.

## Channels

| Channel | Direction / Purpose | Primary Publishers | Primary Subscribers |
|---|---|---|---|
| `hardware:in` | Commands to the race-control owner (hardware monitor) | `franklin-tui.py`, `franklin-gui.py`, `referee_web_app.py` | `rust/franklin-hardware-monitor` (`command_handler_task`) |
| `hardware:out` | **Hardware-only** telemetry/events (or simulation of those same hardware events) | `rust/franklin-hardware-monitor` | `franklin-tui.py`, `franklin-gui.py`, `scoreboard_web_app.py`, `referee_web_app.py`, `healthcheck_web_app.py` (heartbeat sampling), rust local monitor TUI |
| `franklin:events` | Race-control outcome events (`race_control`) | `rust/franklin-hardware-monitor` | `franklin-tui.py`, `franklin-gui.py`, `scoreboard_web_app.py`, `referee_web_app.py`, rust local monitor TUI |
| `franklin:race_state` | Periodic race-state snapshots for observers | `franklin-tui.py`, `franklin-gui.py` | (No in-repo subscriber currently) |

---

## Message contracts

## 1) Commands on `hardware:in`

Envelope:

```json
{
  "type": "command",
  "command": "start_race"
}
```

Optional metadata fields currently accepted by the Rust owner and forwarded when present:

- `command_id` (string)
- `source` (string) *(currently sent by `referee_web_app.py`; tolerated/ignored by Rust owner)*
- `timestamp` (string) *(currently sent by `referee_web_app.py`; tolerated/ignored by Rust owner)*

Supported commands:

- `start_race`
- `end_race`
- `reset_race`
- `simulate_lap` *(simulation/harness use)*
- `add_penalty`
  - fields: `racer_id` (required), `penalty_seconds` (required positive multiple of 5), optional `reason`
- `remove_lap`
  - fields: `racer_id` (required), optional `lap_number` (> 0), optional `reason`
- `disqualify_racer`
  - fields: `racer_id` (required), optional `reason`

Notes:

- Unknown commands are ignored by the Rust owner except for logging (`error!("Unknown command")`).
- There is currently **no** rejection event emitted for unknown commands.

## 2) Telemetry/events on `hardware:out`

Produced by `OutMessage` (Rust).

Channel policy:

- `hardware:out` is reserved for things the lap-counting hardware does, or simulation of those same things.
- Non-hardware control outcomes are published on `franklin:events`.

### `heartbeat`

```json
{"type":"heartbeat","simulated":false}
```

### `status`

```json
{"type":"status","message":"...","simulated":false}
```

### `lap`

```json
{"type":"lap","racer_id":1,"sensor_id":1,"race_time":12.345,"simulated":false}
```

### `error`

```json
{"type":"error","message":"...","simulated":false}
```

### `debug`

```json
{"type":"debug","message":"...","simulated":false}
```

### `raw`

Schema exists in Rust `OutMessage`, but `Raw` is intentionally **not published** to Redis in current code path.

`simulated` semantics on hardware events:

- `true`: event came from simulator path (or `simulate_lap` command path)
- `false`: event came from real hardware path

## 3) Race-control outcomes on `franklin:events`

Produced by Rust `OutMessage::RaceControl`:

```json
{
  "type": "race_control",
  "command": "add_penalty",
  "command_id": "optional-id",
  "accepted": true,
  "message": "Penalty accepted",
  "racer_id": 2,
  "penalty_seconds": 5,
  "reason": "optional",
  "lap_number": 3
}
```

Fields:

- Required: `type`, `command`, `accepted`
- Optional: `command_id`, `message`, `racer_id`, `penalty_seconds`, `reason`, `lap_number`

## 4) Race snapshots on `franklin:race_state`

Published by both TUI and GUI roughly once per second while race is running.

Common fields:

```json
{
  "type": "race_state",
  "timestamp": 12345.67,
  "race_state": "RUNNING",
  "elapsed_time": 42.1,
  "race_mode": "REAL",
  "total_laps": 10
}
```

Additional fields from TUI (non-training mode):

- `racers` (name + completed_laps list)
- `remaining_laps`

---

## Publisher/subscriber map by application

## `rust/franklin-hardware-monitor`

- **Subscribes:** `hardware:in`
- **Also listens (local monitor UI path):** `hardware:out`, `franklin:events`
- **Publishes:**
  - `hardware:out` (`heartbeat`, `status`, `lap`, `error`, `debug`)
  - `franklin:events` (`race_control`)

## `franklin-tui.py`

- **Subscribes:** `hardware:out`, `franklin:events`
- **Publishes:**
  - `hardware:in` (`start_race`, `end_race`)
  - `franklin:race_state`

## `franklin-gui.py`

- **Subscribes:** `hardware:out`, `franklin:events`
- **Publishes:**
  - `hardware:in` (`start_race`, `end_race`, `reset_race`, `countdown_phase`)
  - `franklin:race_state`

## `referee_web_app.py`

- **Subscribes:** `hardware:out`, `franklin:events`
- **Publishes:** `hardware:in` (`start_race`, `end_race`, `reset_race`, `add_penalty`, `remove_lap`, `disqualify_racer`)
- Adds metadata by default to command payloads: `command_id`, `source`, `timestamp`

## `scoreboard_web_app.py`

- **Subscribes:** `hardware:out`, `franklin:events`
- **Publishes to Redis:** none
- **Forwards to browser clients:** all received Redis JSON messages over WebSocket

## `healthcheck_web_app.py`

- **Subscribes:** `hardware:out` (temporary heartbeat sampling only)
- **Publishes:** none

---

## Inconsistencies observed (as of 2026-06-09)

1. **`countdown_phase` command is published by GUI but not handled by Rust owner.**
   - Source: `franklin-gui.py` uses `publish_command("countdown_phase", ...)`.
   - Rust command handler has no `countdown_phase` match arm.

2. **Command envelope metadata is not uniform across producers.**
   - `referee_web_app.py` sends `command_id` / `source` / `timestamp`.
   - TUI/GUI do not include these fields for start/end/reset.

3. **Race-control event shape differs from older design docs.**
   - Current event uses `message` for error/acceptance details.
   - Some older docs mention an `error` field and event `timestamp`; current Rust `RaceControl` schema does not include those.

4. **`franklin:race_state` has no in-repo subscriber today.**
   - Produced by TUI/GUI, currently unused by other in-repo services.

5. **`race_mode` value format differs between TUI and GUI snapshots.**
   - TUI publishes `self.race_mode.name` (likely uppercase enum names).
   - GUI publishes `self.race_mode.value` (value string).

6. **`timestamp` semantics vary by publisher.**
   - `referee_web_app.py` command timestamps are ISO-8601 UTC strings.
   - TUI/GUI race-state timestamps use monotonic float seconds.

7. **Stale/incorrect protocol details existed in docs before this file.**
   - Example: some docs said scoreboard only subscribes to `hardware:out`; code subscribes to `hardware:out` and `franklin:events`.

---

## Maintenance rule

When changing Redis channels, message schemas, or publisher/subscriber responsibilities:

1. Update code.
2. Update **this file**.
3. Update any user-facing docs to link here instead of duplicating protocol details.
