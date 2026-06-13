# Redis Message & Channel Reference (Canonical)

This is the **single source of truth** for Redis pub/sub channels and message contracts in Franklin Lap Counter.

If another document or code comment conflicts with this one, treat this file as authoritative and update the other location.

## Channels

| Channel | Direction / Purpose | Primary Publishers | Primary Subscribers |
|---|---|---|---|
| `hardware:in` | Commands to the race-control owner (hardware monitor) | `franklin-tui.py`, `franklin-gui.py`, `referee_web_app.py` | `rust/franklin-hardware-monitor` (`command_handler_task`), `franklin-race-recorder.py` (caches `start_race` config only) |
| `hardware:out` | **Hardware-only** telemetry/events (or simulation of those same hardware events) | `rust/franklin-hardware-monitor` | `franklin-tui.py`, `franklin-gui.py`, `franklin-race-recorder.py`, `scoreboard_web_app.py`, `referee_web_app.py`, `healthcheck_web_app.py` (heartbeat sampling), rust local monitor TUI |
| `franklin:events` | Race-control + countdown timeline events (`race_control`, `countdown_phase`) | `rust/franklin-hardware-monitor` | `franklin-tui.py`, `franklin-gui.py`, `franklin-race-recorder.py`, `scoreboard_web_app.py`, `referee_web_app.py`, rust local monitor TUI |
| `franklin:race_state` | Authoritative full race-state snapshots (model + leaderboard + persistence-derived state); retained latest at key `franklin:race_state:latest` | `franklin-race-recorder.py` | `franklin-gui.py`, `franklin-tui.py`, other read-only views |

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
  - optional race-config fields consumed by the headless recorder (ignored by the Rust owner): `race_mode` (RaceMode value string), `total_laps` (int), `race_end_mode` (RaceEndMode value string). The recorder caches these (keyed by `command_id`/`start_at`) and applies them when it sees the authoritative `hardware:out` `start_race` event.
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
- `request_status`
  - Request the current status of the hardware monitor (version, simulation mode, connection state). Responsive event is published on `hardware:out` as a `hardware_status` type.

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

### `hardware_status`

```json
{
  "type": "hardware_status",
  "version": "0.2.0",
  "simulation_mode": false,
  "hardware_connected": true,
  "recorded_at": 1736200010.123
}
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

Authoritative, full race-state snapshots published by the headless race recorder
(`franklin-race-recorder.py`, owner of the `Race` model + SQLite writes). GUI/TUI
and other read-only views render these instead of maintaining their own model.

Publishing pattern (recorder):

1. `SET franklin:race_state:latest <json>` — retained "latest" snapshot for late joiners.
2. `PUBLISH franklin:race_state <json>` — live update.

Subscriber pattern (GUI/TUI/views):

1. Subscribe to `franklin:race_state`.
2. `GET franklin:race_state:latest` once on startup and render it.
3. Ignore any snapshot whose `snapshot_seq` is `<=` the last applied one.

Snapshot envelope (`schema_version: 1`):

```json
{
  "schema_version": 1,
  "snapshot_seq": 42,
  "snapshot_at": 1736200012.600,
  "recorder_id": "9f2c…",

  "state": "running",
  "race_id": 12,
  "start_at": 1736200000.250,
  "end_at": null,
  "elapsed_seconds": 12.350,

  "race_mode": "Real Race Mode",
  "total_laps": 10,
  "effective_total_laps": 10,
  "race_end_mode": "last_car",

  "leaderboard": [
    {
      "position": 1,
      "racer_id": 3,
      "lap_count": 5,
      "best_lap_time": 4.92,
      "last_lap_time": 5.10,
      "raw_total_time": 25.7,
      "penalty_seconds": 0,
      "adjusted_total_time": 25.7,
      "disqualified": false
    }
  ],
  "laps_remaining": { "leader": 5, "last_place": 7 },

  "penalties": { "2": 5 },
  "disqualified": [4],

  "laps": [
    {
      "racer_id": 3,
      "lap_number": 5,
      "lap_time": 5.10,
      "race_time": 25.7,
      "race_start_at": 1736200000.250,
      "lap_at": 1736200025.950,
      "recorded_at": 1736200025.960
    }
  ]
}
```

Field semantics:

- `state`: lowercased `RaceState` name — `not_started`, `running`, `winner_declared`, `finished`, `paused`.
- `snapshot_seq`: monotonically increasing per recorder run; clients drop stale/out-of-order snapshots.
- `recorder_id`: opaque id unique to each recorder run. When it changes (recorder restart, where `snapshot_seq` resets to 1) clients fall back to the newer `snapshot_at` instead of treating the fresh run's low sequence number as stale.
- Clock: `start_at`/`end_at` are epoch seconds; `elapsed_seconds` is authoritative render state. Clients should render `elapsed_seconds` and, while `state` is `running`/`winner_declared`, advance it locally using their own monotonic clock between snapshots; freeze it otherwise.
- `total_laps` is the user-facing setting; `effective_total_laps` is what the model uses (e.g. Training maps to a very large target). Clients hide "laps remaining" when `race_mode` is Training.
- `race_end_mode`: effective `RaceEndMode` value (`winner`, `last_car`, `manual`).
- `leaderboard` rows are display-ready and already sorted (active rows first by `adjusted_total_time`, then DQ rows). Missing best/last lap times are `null` (never `Infinity`).
- `penalties` keys are racer IDs as strings (JSON object keys).
- Names and colors are **not** in the snapshot; views look those up locally by `racer_id`.

If `franklin:race_state:latest` is absent (recorder not yet running), views fall back to a neutral idle state.

---

## Publisher/subscriber map by application

## `rust/franklin-hardware-monitor`

- **Subscribes:** `hardware:in`
- **Also listens (local monitor UI path):** `hardware:out`, `franklin:events`
- **Publishes:**
  - `hardware:out` (`heartbeat`, `status`, `lap`, `error`, `debug`, `start_race`)
  - `franklin:events` (`race_control`, `countdown_phase`)

## `franklin-tui.py` *(pure renderer — no DB writes)*

- **Subscribes:** `hardware:out`, `franklin:events`, `franklin:race_state` (and reads `franklin:race_state:latest` on connect)
- **Publishes:**
  - `hardware:in` (`start_race` with schedule fields, `end_race`)
- Renders the authoritative `franklin:race_state` snapshot; it never owns a `Race` model or writes SQLite. `hardware:out`/`franklin:events` are used display-only (heartbeat, countdown notifications, log lines).

## `franklin-gui.py` *(pure renderer — no DB writes)*

- **Subscribes:** `hardware:out`, `franklin:events`, `franklin:race_state` (and reads `franklin:race_state:latest` on connect)
- **Publishes:**
  - `hardware:in` (`start_race` with schedule fields, `end_race`, `reset_race`)
- Renders the authoritative `franklin:race_state` snapshot; it never owns a `Race` model or writes SQLite. `hardware:out`/`franklin:events` are used display-only (heartbeat, countdown/start lights, log lines).

## `franklin-race-recorder.py` *(headless recorder — sole race-model owner & DB writer)*

- **Subscribes:** `hardware:out`, `franklin:events`, `hardware:in` (the latter only to cache `start_race` race-config)
- **Publishes:**
  - `franklin:race_state` (full snapshots) and sets `franklin:race_state:latest`
  - `hardware:in` (`end_race`) when it detects automatic race finish
- Owns the in-memory `Race` model and is the only writer to SQLite (`laps`, `races`).
- Core model/persistence logic lives in `race/race_engine.py` (`RaceEngine`); the daemon is a thin Redis transport around it.

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

## Inconsistencies observed

There are currently no known in-repo contract inconsistencies.

Recently resolved:

- command envelope metadata is now uniform across in-repo Python producers via `redis_commands.py`
- race-control event documentation has been aligned to the current `message` + `recorded_at` schema
- stale/duplicated protocol notes in secondary docs were cleaned up to reference this canonical file

---

## Maintenance rule

When changing Redis channels, message schemas, or publisher/subscriber responsibilities:

1. Update code.
2. Update **this file**.
3. Update any user-facing docs to link here instead of duplicating protocol details.
