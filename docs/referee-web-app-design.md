# Referee Web App: Architecture Notes

Status: baseline implemented; this document focuses on referee feature intent and rollout notes.

> Redis channels/message schemas are canonical in `docs/redis-message-reference.md`.
> Do not duplicate protocol payload details here.

## Implemented baseline

- `referee_web_app.py` provides:
  - REST control endpoints (`start_race`, `end_race`, `reset_race`, `add_penalty`, `remove_lap`, `disqualify_racer`)
  - WebSocket stream for live events
  - audit read endpoint (`/api/control/audit`)
- `rust/franklin-hardware-monitor/src/main.rs` owns command handling and emits race-control outcomes.
- `franklin-tui.py` and `franklin-gui.py` consume race-control outcomes and apply local display-state adjustments.
- SQLite audit table `race_control_actions` is populated from race-control events.

## Goal

Provide race officials a dedicated UI to issue race-control decisions while keeping one authoritative command owner in the system.

## System responsibilities (high level)

- **Authoritative owner:** `rust/franklin-hardware-monitor`
- **Operational race UIs:** `franklin-tui.py`, `franklin-gui.py`
- **Referee operator UI:** `referee_web_app.py`
- **Spectator UI bridge:** `scoreboard_web_app.py`
- **Operational diagnostics:** `healthcheck_web_app.py`

For exact publish/subscribe mappings and message fields, see:

- `docs/redis-message-reference.md`

## Remaining gaps / follow-up ideas

- Consider explicit rejection events for unknown commands (currently logged only by owner).
- Add stronger authz for referee actions if needed outside trusted networks.

## Recently resolved

- Command metadata envelope is now consistent across Python producers via `redis_commands.py` (`command_id`, `source`, `timestamp`).
- `countdown_phase` and scheduled `start_race` timeline events are implemented by the command owner and published on `franklin:events`.
- `franklin:race_state` periodic snapshot publishing has been retired for now.

## Data model notes

Current persistent audit is `race_control_actions`.

Potential future extensions (if needed):

- explicit penalty ledger table
- explicit disqualification table
- explicit lap-adjustment table

These may remain derived from the append-only audit stream unless query/perf needs justify materialized tables.
