# Redis Message & Channel Reference (Canonical)

This is the **single source of truth** for Redis pub/sub channels and message contracts in Franklin Lap Counter.

If another document or code comment conflicts with this one, treat this file as authoritative and update the other location.

## Channels

| Channel | Direction / Purpose | Primary Publishers | Primary Subscribers |
|---|---|---|---|
| `hardware:in` | Commands to the race-control owner (hardware monitor) | `franklin-tui.py`, `franklin-gui.py`, `referee_web_app.py` | `rust/franklin-hardware-monitor` (`command_handler_task`) |
| `hardware:out` | **Hardware-only** telemetry/events (or simulation of those same hardware events) | `rust/franklin-hardware-monitor` | `franklin-tui.py`, `franklin-gui.py`, `scoreboard_web_app.py`, `referee_web_app.py`, `healthcheck_web_app.py` (heartbeat sampling), rust local monitor TUI |
| `franklin:events` | Race-control + countdown timeline events (`race_control`, `countdown_phase`) | `rust/franklin-hardware-monitor` | `franklin-tui.py`, `franklin-gui.py`, `scoreboard_web_app.py`, `referee_web_app.py`, rust local monitor TUI |
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

Standard metadata fields on all in-repo command producers:

- `command_id` (string UUID)
- `source` (string producer id)
- `timestamp` (ISO-8601 UTC string)

All Python command producers (`franklin-tui.py`, `franklin-gui.py`, `referee_web_app.py`) use shared helpers in `redis_commands.py` to generate/validate this envelope.

Supported commands:

- `start_race`
  - supports synchronized schedule fields: `ready_at`, `set_at`, `go_at`, `start_at` (epoch seconds, float)
  - if omitted, owner falls back to immediate start timing
- `end_race`
- `reset_race`
- `simulate_lap` *(simulation/harness use)*
  - fields: optional `racer_id`, optional `sensor_id`, optional `race_time` (relative seconds for simulator input)
  - emitted lap events on `hardware:out` still use epoch fields: `race_start_at`, `lap_at`, `recorded_at`
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
{"type":"heartbeat","recorded_at":1736200010.000,"simulated":false}
```

### `status`

```json
{"type":"status","message":"...","recorded_at":1736200010.123,"simulated":false}
```

### `lap`

```json
{
  "type":"lap",
  "racer_id":1,
  "sensor_id":1,
  "lap_number":3,
  "race_start_at":1736200000.250,
  "lap_at":1736200012.595,
  "recorded_at":1736200012.600,
  "simulated":false
}
```

### `error`

```json
{"type":"error","message":"...","recorded_at":1736200010.456,"simulated":false}
```

### `debug`

```json
{"type":"debug","message":"...","recorded_at":1736200010.789,"simulated":false}
```

### `start_race`

```json
{"type":"start_race","at":1736200000.250,"recorded_at":1736199998.250,"command_id":"optional-id","source":"franklin_tui","simulated":false}
```

### `raw`

Schema exists in Rust `OutMessage`, but `Raw` is intentionally **not published** to Redis in current code path.

`simulated` semantics on hardware events:

- `true`: event came from simulator path (or `simulate_lap` command path)
- `false`: event came from real hardware path

Lap time semantics:

- `lap_at`, `race_start_at`, and `recorded_at` are Unix epoch seconds (authoritative absolute timeline)
- `lap_number` is per-racer sequence count from lap stream (does not include penalties)
- If a consumer needs race-relative seconds, compute `lap_at - race_start_at`

## 3) Control timeline events on `franklin:events`

Produced by Rust owner (`OutMessage::RaceControl` and `OutMessage::CountdownPhase`).

### `countdown_phase`

```json
{"type":"countdown_phase","phase":"ready","at":1736200000.250,"recorded_at":1736199998.250,"command_id":"optional-id","source":"franklin_tui"}
```

Phases emitted for scheduled starts: `ready`, `set`, `go`.

### `race_control`

```json
{
  "type": "race_control",
  "command": "add_penalty",
  "command_id": "optional-id",
  "recorded_at": 1736200000.500,
  "accepted": true,
  "message": "Penalty accepted",
  "racer_id": 2,
  "penalty_seconds": 5,
  "reason": "optional",
  "lap_number": 3
}
```

Fields:

- Required: `type`, `command`, `recorded_at`, `accepted`
- Optional: `command_id`, `message`, `racer_id`, `penalty_seconds`, `reason`, `lap_number`

## 4) Race snapshots on `franklin:race_state`

Published by both TUI and GUI roughly once per second while race is running.

`timestamp` is Unix epoch seconds.

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
  - `hardware:out` (`heartbeat`, `status`, `lap`, `error`, `debug`, `start_race`)
  - `franklin:events` (`race_control`, `countdown_phase`)

## `franklin-tui.py`

- **Subscribes:** `hardware:out`, `franklin:events`
- **Publishes:**
  - `hardware:in` (`start_race` with schedule fields, `end_race`)
  - `franklin:race_state`

## `franklin-gui.py`

- **Subscribes:** `hardware:out`, `franklin:events`
- **Publishes:**
  - `hardware:in` (`start_race` with schedule fields, `end_race`, `reset_race`)
  - `franklin:race_state`

## `referee_web_app.py`

- **Subscribes:** `hardware:out`, `franklin:events`
- **Publishes:** `hardware:in` (`start_race` with schedule fields, `end_race`, `reset_race`, `add_penalty`, `remove_lap`, `disqualify_racer`)
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

> Resolved since this snapshot:
> - command envelope metadata is now uniform across in-repo Python producers via `redis_commands.py`
> - race-control event documentation has been aligned to the current `message` + `recorded_at` schema

1. **`franklin:race_state` has no in-repo subscriber today.**
   - Produced by TUI/GUI, currently unused by other in-repo services.

2. **`race_mode` value format differs between TUI and GUI snapshots.**
   - TUI publishes `self.race_mode.name` (likely uppercase enum names).
   - GUI publishes `self.race_mode.value` (value string).

3. **Command timestamp field formats vary by channel/publisher.**
   - `referee_web_app.py` command payloads use ISO-8601 UTC strings on `hardware:in`.
   - TUI/GUI `franklin:race_state.timestamp` is Unix epoch seconds.

4. **Stale/incorrect protocol details existed in docs before this file.**
   - Example: some docs said scoreboard only subscribes to `hardware:out`; code subscribes to `hardware:out` and `franklin:events`.

---

## Maintenance rule

When changing Redis channels, message schemas, or publisher/subscriber responsibilities:

1. Update code.
2. Update **this file**.
3. Update any user-facing docs to link here instead of duplicating protocol details.
