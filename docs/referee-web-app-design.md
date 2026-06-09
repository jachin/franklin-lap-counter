# Referee Web App: Architecture and Message Contract (Draft)

Status: draft for implementation planning.

## Goal

Add a second web app for race officials (referees) that can:

- Start race
- Stop race
- Reset race
- Remove invalid lap(s) during a race
- Add penalty time in 5-second increments during a race
- Disqualify racers

All actions are coordinated through Redis so every consumer can react consistently.

## Current system (as implemented today)

### Components

- `rust/franklin-hardware-monitor/src/main.rs`
  - Publishes hardware telemetry/events to Redis channel `hardware:out`
  - Subscribes to command channels `race:control` (primary) and `hardware:in` (legacy)
- `franklin-tui.py`
  - Subscribes to `hardware:out`
  - Publishes race start/end commands to `race:control`
  - Publishes race state snapshots to `franklin:race_state`
- `franklin-gui.py`
  - Subscribes to `hardware:out`
  - Publishes race start/end/reset (and countdown phase) commands to `race:control`
  - Publishes race state snapshots to `franklin:race_state`
- `scoreboard_web_app.py`
  - Subscribes to `hardware:out`
  - Broadcasts those events to web clients via WebSocket

### Existing command contract

Current command payload shape (published to `race:control`; `hardware:in` still accepted for compatibility):

```json
{
  "type": "command",
  "command": "start_race"
}
```

Handled by hardware monitor today:

- `start_race`
- `end_race`
- `simulate_lap`

Important note:

- Canonical race-end command is `end_race`.
- GUI reset now publishes `reset_race` and all consumers can react via `race_control` events on `hardware:out`.

### Existing event contract

Current event types on `hardware:out`:

- `heartbeat`
- `status`
- `lap` (`racer_id`, `sensor_id`, `race_time`)
- `error`
- `debug`

## Gaps vs referee requirements

The current model does not provide authoritative race-control events for:

- `reset_race`
- `remove_lap`
- `add_penalty`
- `disqualify_racer`

It also lacks persistence structures for race officiating decisions (invalidated laps, penalties, DQ reason/source).

## Proposed Redis contract (v1 for referee feature)

Use dedicated command ingress on `race:control`, and keep legacy `hardware:in` as a compatibility input during transition. Emit explicit race-control events on `hardware:out` so all subscribers can apply the same decision stream.

### Common envelope

```json
{
  "type": "command",
  "command": "...",
  "command_id": "uuid",
  "source": "referee_web_app",
  "timestamp": "2026-06-09T12:34:56Z"
}
```

Fields:

- `command_id`: idempotency + audit correlation
- `source`: producer identity (`franklin_tui`, `franklin_gui`, `referee_web_app`, etc.)
- `timestamp`: producer time (ISO-8601)

### Commands

#### 1) Start race

```json
{
  "type": "command",
  "command": "start_race",
  "command_id": "...",
  "source": "referee_web_app",
  "timestamp": "..."
}
```

#### 2) End race

Use canonical `end_race` everywhere.

```json
{
  "type": "command",
  "command": "end_race",
  "command_id": "...",
  "source": "referee_web_app",
  "timestamp": "..."
}
```

#### 3) Reset race

```json
{
  "type": "command",
  "command": "reset_race",
  "command_id": "...",
  "source": "referee_web_app",
  "timestamp": "..."
}
```

#### 4) Remove lap (invalidate lap)

Prefer targeting by `lap_id` (stable), fallback `(racer_id, lap_number)`.

```json
{
  "type": "command",
  "command": "remove_lap",
  "command_id": "...",
  "source": "referee_web_app",
  "timestamp": "...",
  "lap_id": 123,
  "reason": "cut track"
}
```

Fallback form:

```json
{
  "type": "command",
  "command": "remove_lap",
  "command_id": "...",
  "source": "referee_web_app",
  "timestamp": "...",
  "racer_id": 2,
  "lap_number": 5,
  "reason": "cut track"
}
```

#### 5) Add penalty time

Penalty is additive; enforce 5-second increments at API/UI layer.

```json
{
  "type": "command",
  "command": "add_penalty",
  "command_id": "...",
  "source": "referee_web_app",
  "timestamp": "...",
  "racer_id": 2,
  "penalty_seconds": 5,
  "reason": "unsafe pit exit"
}
```

#### 6) Disqualify racer

```json
{
  "type": "command",
  "command": "disqualify_racer",
  "command_id": "...",
  "source": "referee_web_app",
  "timestamp": "...",
  "racer_id": 3,
  "reason": "technical violation"
}
```

### Resulting events on `hardware:out`

To keep consumers synchronized, command handlers should emit authoritative events:

- `race_control`

Example:

```json
{
  "type": "race_control",
  "command": "add_penalty",
  "command_id": "...",
  "accepted": true,
  "racer_id": 2,
  "penalty_seconds": 5,
  "timestamp": "..."
}
```

On rejection:

```json
{
  "type": "race_control",
  "command": "remove_lap",
  "command_id": "...",
  "accepted": false,
  "error": "lap not found",
  "timestamp": "..."
}
```

## Consumer responsibilities (planned)

### `franklin-hardware-monitor` (Rust)

- Parse/validate new commands from `race:control` (and legacy `hardware:in` during transition)
- Continue hardware responsibilities (`start_race`, reset signaling)
- Publish `race_control` accepted/rejected events
- Backward compatibility:
  - continue accepting legacy `hardware:in` while migrating producers to `race:control`

### `franklin-tui.py` and `franklin-gui.py`

- Handle new `race_control` events from `hardware:out`
- Update local race state deterministically for:
  - lap removal
  - penalty accumulation
  - DQ
- Prefer emitting canonical `end_race`

### `scoreboard_web_app.py`

- Forward new `race_control` events to clients
- Render penalty/DQ/invalidated-lap state in leaderboard and event feed

### New `referee_web_app.py`

- Authenticate/authorize referee actions (if needed)
- Validate command input
- Publish command envelopes to `race:control`
- Show command result stream (`race_control` accepted/rejected)

## Data model changes required

Current SQLite schema (`database.py`) stores races/laps only.

Add tables (or equivalent) for officiating actions:

- `race_penalties`
  - `id`, `race_id`, `racer_id`, `penalty_seconds`, `reason`, `command_id`, `created_at`
- `race_disqualifications`
  - `id`, `race_id`, `racer_id`, `reason`, `command_id`, `created_at`
- `lap_adjustments`
  - `id`, `race_id`, `lap_id` (nullable), `racer_id`, `lap_number` (nullable), `action` (`invalidate`), `reason`, `command_id`, `created_at`

This enables replay/audit and deterministic rebuild of leaderboard state.

## Suggested implementation phases

1. **Protocol + compatibility**
   - Introduce command envelope and `race_control` events
   - Use canonical `end_race` in all producers
2. **State model**
   - Extend race model for penalties, lap invalidation, and DQ
   - Add DB schema + persistence methods
3. **Consumer updates**
   - Apply `race_control` events in GUI/TUI/scoreboard
4. **Referee web app**
   - Basic UI + API for action dispatch
   - Action log + error handling
5. **Hardening**
   - Idempotency checks via `command_id`
   - Test suite for race-control scenarios

## Open decisions

- Should authoritative race-control processing live in one owner process (recommended), or be shared among UIs?
- Should `hardware:out` continue as mixed telemetry + control events, or split with a new `franklin:events` channel?
- What are final ranking rules when penalties and DQ both exist?
- Can penalties be removed/edited, and by whom?

---

If this design direction looks right, the next step is to implement **Phase 1** with minimal breakage (publish to `race:control`, keep legacy `hardware:in` input support, and emit `race_control` events).
