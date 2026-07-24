# Referee Web App API

The Referee Web App provides a REST API and a WebSocket stream for race control and monitoring.

## Base URL
The default port is `8081`.
Example: `http://<pi-ip>:8081`

---

## 1. Static Content

### Dashboard
- **URL:** `GET /`
- **Description:** Serves the main Referee interface (`referee.html`).

---

## 2. Configuration & Health

### Health Check
- **URL:** `GET /api/health`
- **Response:** `{"ok": true}`

### Get Configuration
- **URL:** `GET /api/config`
- **Description:** Retrieves current race configuration and preferences from the SQLite database.
- **Response Example:**
```json
{
  "race_mode": "Real Race Mode",
  "total_laps": 10,
  "race_end_mode": "last_car",
  "contestants": [
    {"transmitter_id": 1, "name": "Racer 1", "color": "#ff0000"},
    {"transmitter_id": 2, "name": "Racer 2", "color": "#0000ff"}
  ]
}
```

---

## 3. Race Control API

All race control endpoints are `POST` requests and return a JSON response in the format `{"ok": true, "published": <payload>}` on success.

### Common Error Responses
- **409 Conflict:** Returned if an action is attempted that is invalid for the current race state (e.g., trying to pause when no race is running).
  - Body: `{"ok": false, "error": "No race is currently in progress"}`

### Start Race
- **URL:** `POST /api/control/start_race`
- **Body:**
```json
{
  "race_mode": "Real Race Mode", // Optional
  "total_laps": 10,              // Optional
  "operator": "Name"             // Optional, for audit logging
}
```
- **Description:** Schedules a new race with a 4-phase countdown (`ready1` -> `ready2` -> `set` -> `go`).

### End Race
- **URL:** `POST /api/control/end_race`
- **Body:** `{"operator": "Name"}` (optional)
- **Description:** Ends the current race immediately.

### Pause Race
- **URL:** `POST /api/control/pause_race`
- **Body:** `{"operator": "Name"}` (optional)
- **Description:** Pauses the current race.

### Resume Race
- **URL:** `POST /api/control/resume_race`
- **Body:** `{"operator": "Name"}` (optional)
- **Description:** Resumes a paused race.

### Reset Race
- **URL:** `POST /api/control/reset_race`
- **Body:** `{"operator": "Name"}` (optional)
- **Description:** Resets the race system to the idle state.

### Add Penalty
- **URL:** `POST /api/control/add_penalty`
- **Body:**
```json
{
  "racer_id": 1,
  "penalty_seconds": 5,
  "reason": "Jumped start",
  "operator": "Name"
}
```
- **Description:** Adds a time penalty to a racer. `penalty_seconds` must be a positive multiple of 5.

### Remove Lap
- **URL:** `POST /api/control/remove_lap`
- **Body:**
```json
{
  "racer_id": 1,
  "lap_number": 3,      // Optional; if omitted, removes the latest lap
  "reason": "Missed gate",
  "operator": "Name"
}
```
- **Description:** Removes a recorded lap from a racer.

### Disqualify Racer
- **URL:** `POST /api/control/disqualify_racer`
- **Body:**
```json
{
  "racer_id": 1,
  "reason": "Unsportsmanlike conduct",
  "operator": "Name"
}
```
- **Description:** Disqualifies a racer from the current race.

---

## 4. Audit & History

### Audit Log
- **URL:** `GET /api/control/audit`
- **Query Parameters:**
  - `race_id`: (optional) Filter by a specific race ID.
  - `limit`: (optional, default 100) Max number of records to return (capped at 500).
- **Description:** Retrieves the history of race control actions from the database.
- **Response Example:**
```json
{
  "ok": true,
  "count": 1,
  "actions": [
    {
      "id": 1,
      "race_id": 12,
      "command": "add_penalty",
      "operator": "Jachin",
      "accepted": true,
      "payload": "{...}",
      "recorded_at": "2026-07-24T00:00:00Z"
    }
  ]
}
```

---

## 5. WebSocket API

- **URL:** `GET /ws`
- **Description:** Real-time event stream providing live race updates.

### Connection
Upon connecting, the server sends a welcome message:
```json
{"type": "connected", "message": "Referee WebSocket connected"}
```
If a race is in progress, it immediately follows with the latest authoritative snapshot from the `franklin:race_state` channel.

### Live Updates
The WebSocket forwards all JSON messages received from the following Redis channels:
- `hardware:out`: Hardware telemetry (heartbeats, laps, sensor signals).
- `franklin:events`: Race control confirmations and countdown phases.
- `franklin:race_state`: Authoritative full race-state snapshots.

For detailed message schemas, see [docs/redis-message-reference.md](redis-message-reference.md).
