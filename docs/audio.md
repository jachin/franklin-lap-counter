# Audio System

## Architecture

Audio is **client-side only** — all sound is generated at runtime (in the browser via the Web Audio API, and in the GTK GUI via synthesized WAV + ALSA `aplay`). No audio files are shipped. The tone definitions live in a single shared patch file, `static/sounds.json`, consumed by both `audio.js` and `franklin-gui.py`.

The audio pipeline is:

```
Redis               WebSocket                Browser
─────────────────────────────────────────────────────────────
hardware:out  ──┐
franklin:events ─┤──► web_app.py ──────► WebSocket ──► onmessage handler ──► FranklinAudio.play*()
franklin:race_   │     (broadcasts all  (ws://host/ws)                     (module dispatches by
  state          ┘      Redis JSON msgs)                                     soundStyle internally)
```

Each static page has its own Python web app that subscribes to `hardware:out`, `franklin:events`, and `franklin:race_state`, then forwards every JSON message verbatim to connected browser clients. The `FranklinAudio` module (`static/audio.js`) handles all sound generation and style dispatch — pages just call `FranklinAudio.playReady()`, `FranklinAudio.playSet()`, etc.

## Files with Audio

| File | Type | Role |
|------|------|------|
| `static/audio.js` | Client-side JS module | **Reader of the shared patch.** Synthesizes all web race sounds via Web Audio API from `static/sounds.json`. |
| `static/sounds.json` | Shared data (JSON) | **Single source of truth for all tone definitions** (classic + 8bit). Consumed by both `audio.js` and `franklin-gui.py`. |
| `franklin-gui.py` | GTK4 GUI (Python) | Reads the `classic` block of `sounds.json`, synthesizes WAV with the stdlib, and plays it via ALSA `aplay` (background thread). No Web Audio API available in GTK. |
| `static/driver.html` | Client-side page | Includes `audio.js`; triggers sounds from its WebSocket handler. |
| `static/index.html` | Client-side page (scoreboard) | Includes `audio.js`; triggers sounds from its WebSocket handler. |
| `static/referee.html` | Client-side page (ref) | Includes `audio.js`; triggers sounds from its WebSocket handler. |
| `driver_web_app.py` | Server (Python) | WebSocket proxy that feeds Redis messages to the driver page. |
| `scoreboard_web_app.py` | Server (Python) | WebSocket proxy that feeds Redis messages to the scoreboard page. |
| `referee_web_app.py` | Server (Python) | WebSocket proxy that feeds Redis messages to the referee page. |
| `docs/redis-message-reference.md` | Documentation | Canonical reference for Redis message contracts. |

## FranklinAudio Module API

### Public Methods

| Method | Description |
|--------|-------------|
| `init()` | Create/resume `AudioContext` (returns Promise). Call from user gesture. |
| `isReady()` | Returns `true` if `AudioContext` is initialized and not suspended. |
| `playReady()` | Plays "Ready" countdown sound using current style. |
| `playSet()` | Plays "Set" countdown sound using current style. |
| `playGo()` | Plays "Go" race-start sound using current style. |
| `playFinish()` | Plays race-finish fanfare using current style. |
| `beep(freq, ms)` | Simple sine-wave beep at given frequency for given duration. |
| `setEnabled(bool)` | Enable/disable sound. Persisted in memory only (default `true`). |
| `getEnabled()` | Returns current enabled state. |
| `setStyle("classic"\|"8bit")` | Set sound style. Persisted to `localStorage` under `franklin_sound_style`. |
| `getStyle()` | Returns current style string. |

### Sound Style Dispatch

`FranklinAudio` handles style dispatch internally — pages call the generic method and the module picks the right implementation:

| Page call | Classic implementation | 8-bit implementation |
|-----------|----------------------|---------------------|
| `.playReady()` | Four square-wave chord (174.61, 220.00, 261.63, 349.23 Hz) | Three C3 (130.81 Hz) pulses |
| `.playSet()` | Same four-note chord as ready | G3 pulse + rising square sweep + noise |
| `.playGo()` | Four-note ascending chord (196–392 Hz) | Ascending arpeggio C4→C6 + rev noise |
| `.playFinish()` | Descending pentatonic + noise + horn | Descending pentatonic B5→C4 + noise crash |

## Message Types That Trigger Sound

### `countdown_phase` (from `franklin:events`)

Published by the Rust hardware monitor for scheduled race starts. Three phases are emitted: `ready`, `set`, `go`.

```json
{"type":"countdown_phase","phase":"ready","at":1736200000.250,"recorded_at":1736199998.250}
```

- `phase`: one of `"ready"`, `"set"`, `"go"`
- `at`: epoch-seconds timestamp of when that phase should occur

### `status` (from `hardware:out`)

```json
{"type":"status","message":"Race ended","recorded_at":1736200010.123}
```

Triggers finish sound when `message` contains `"end"` or `"reset"`.

### `race_control` (from `franklin:events`)

```json
{"type":"race_control","command":"end_race","accepted":true,...}
```

Triggers finish sound when `command` is `"end_race"` or `"reset_race"` and `accepted` is `true`.

### `start_race` (from `hardware:out`)

```json
{"type":"start_race","at":1736200000.250,...}
```

Signals that the race has actually started. Used to finalize the transition from countdown to running state.

## Adding Audio to a New Page

### 1. Server-side: WebSocket Proxy

The page needs a WebSocket endpoint that subscribes to `hardware:out` and `franklin:events` (and optionally `franklin:race_state`). See `driver_web_app.py` for reference. The key subscription channels are:

```python
REDIS_OUT_CHANNEL = "hardware:out"
REDIS_EVENTS_CHANNEL = "franklin:events"
# Also subscribe for snapshot data if the page needs race state:
RACE_STATE_CHANNEL = "franklin:race_state"
```

### 2. Client-side: Include the Module

```html
<script src="/static/audio.js"></script>
```

### 3. AudioContext Initialization

Browsers require a user gesture before `AudioContext` can play audio. Add an init modal to the page:

```html
<div id="audioModal" style="position:fixed;inset:0;background:rgba(0,0,0,0.7);display:flex;align-items:center;justify-content:center;z-index:999">
    <div style="background:var(--surface);border:1px solid var(--surface-border);border-radius:12px;padding:24px;max-width:320px;text-align:center">
        <p style="margin:0 0 12px;font-size:1.1rem">🔊 Audio is on</p>
        <p style="margin:0 0 16px;color:var(--text);opacity:0.6;font-size:0.9rem">Race sounds enabled</p>
        <button type="button" id="audioOkBtn" style="width:auto;min-width:100px;margin:0 auto">OK</button>
    </div>
</div>
```

Wire it up:
```js
document.getElementById("audioOkBtn").addEventListener("click", async () => {
    await FranklinAudio.init();
    document.getElementById("audioModal").remove();
    FranklinAudio.beep(440, 120);
});
```

### 4. Sound Controls UI

Add a sound toggle button and style selector:

```html
<button type="button" id="soundBtn">Sound: on</button>
<select id="soundStyleSelect" style="width:auto;min-width:120px">
    <option value="classic">Classic</option>
    <option value="8bit">8-Bit</option>
</select>
```

Wire them up:
```js
document.getElementById("soundStyleSelect").value = FranklinAudio.getStyle();

document.getElementById("soundBtn").addEventListener("click", async () => {
    const nowEnabled = !FranklinAudio.getEnabled();
    FranklinAudio.setEnabled(nowEnabled);
    if (nowEnabled) {
        await FranklinAudio.init();
        document.getElementById("soundBtn").textContent = "Sound: on";
        FranklinAudio.beep(440, 120);
    } else {
        document.getElementById("soundBtn").textContent = "Sound: off";
    }
});

document.getElementById("soundStyleSelect").addEventListener("change", () => {
    FranklinAudio.setStyle(document.getElementById("soundStyleSelect").value);
});
```

### 5. State Required for Countdown Timing

```js
const countdownTimers = [];     // array of setTimeout IDs
let countdownActive = false;    // true between "ready" and "go"
let countdownReadyAt = 0;       // epoch-seconds of the "ready" phase
let countdownReadyAtMs = 0;     // local Date.now() when "ready" was received
```

### 6. Countdown Timing Logic

The Rust hardware monitor schedules race starts by publishing `countdown_phase` messages on `franklin:events`. Each message has an `at` field — the epoch-second time the phase should occur. Because messages may arrive early (published before the phase actually triggers), the client must schedule them relative to the local clock.

**Algorithm:**

```
ready message arrives:
    record countdownReadyAt = msg.at  (epoch-seconds)
    record countdownReadyAtMs = Date.now()  (local ms)
    FranklinAudio.playReady()  ← plays IMMEDIATELY
    set countdownActive = true

set message arrives:
    totalMs = (setAt - countdownReadyAt) * 1000  (expected gap from ready to set)
    elapsedMs = Date.now() - countdownReadyAtMs  (actual wall-clock time since ready)
    remainingMs = max(0, totalMs - elapsedMs)
    setTimeout(() => { if (countdownActive) FranklinAudio.playSet(); }, remainingMs)

go message arrives:
    (same calculation as set)
    setTimeout(() => { if (countdownActive) { FranklinAudio.playGo(); countdownActive = false; } }, remainingMs)
```

This ensures correct timing regardless of network latency.

**Key insight:** The "ready" sound plays immediately, while "set" and "go" are scheduled with `setTimeout` using the difference between the planned epoch offsets and the current wall clock.

### 7. Finish Sound Triggers

The finish sound plays when:

- A `status` message arrives with `message` containing `"end"` or `"reset"` → `FranklinAudio.playFinish()`
- A `race_control` message arrives with `command` being `"end_race"` or `"reset_race"` and `accepted: true` → `FranklinAudio.playFinish()`

On finish, also clear any pending countdown timers and reset `countdownActive` to `false`.

## Sound Styles

The module supports multiple sound styles dispatched internally:

| Style | Character |
|-------|-----------|
| `"classic"` | Sine/square wave tones, long fades |
| `"8bit"` | Pulse wave + bitcrusher, chiptune feel |

Selection is persisted in `localStorage` under key `franklin_sound_style`.

### Adding a New Sound Style

1. Open `static/sounds.json` and add a new top-level style key (e.g. `"chip"`) containing `ready`, `set`, `go`, `finish` entries. Each entry has a `voices` array; optionally a `"bitcrush": true` flag.
2. Each voice is `{ "type": "square"|"sine"|"pulse"|"noise", "freq": <Hz>, "freq_end": <Hz optional ramp>, "start": <s>, "dur": <s>, "attack": <s>, "vol": <0..1> }`.
3. Add the style name to the `<select id="soundStyleSelect">` dropdown on each page that needs it. `audio.js` picks it up automatically via `localStorage`. The GTK GUI only ever renders the `classic` block.

### Dispatch Wiring

Dispatch is now internal to `FranklinAudio` and driven by the JSON patch:

```js
function dispatch(name) {
    if (!soundEnabled || !audioCtx || audioCtx.state === "suspended") return;
    const stylePatch = soundPatch && soundPatch[soundStyle];
    const soundDef = stylePatch && stylePatch[name];
    if (soundDef) playVoices(soundDef);
}
```

`playVoices()` builds one oscillator (or noise buffer) per voice, applies the linear attack/decay envelope, and routes through the bitcrusher chain when `bitcrush` is set. `audio.js` fetches `/static/sounds.json` once in `init()`.

Pages call `FranklinAudio.playXxx()` and the module handles the rest.

## 8-Bit Sound Implementation

### Bitcrusher

Created once via `initBitcrusher()` using a `WaveShaperNode` with a quantization curve:

```js
const curve = new Float32Array(2048);
for (let i = 0; i < 2048; i++) {
    let x = (i / 2047) * 2 - 1;
    curve[i] = Math.round(x * 8) / 8;  // 8 quantization steps
}
```

Output chain: `bitcrusherNode → bitcrusherGainNode (×3.0) → destination`

The `bitcrusherGainNode` at 3.0× compensates for signal attenuation through the quantizer.

### Pulse Wave

Created via `createPeriodicWave()` with a narrow-pulse harmonic series (25% duty cycle, similar to NES/Game Boy pulse channels):

```js
const real = new Float32Array([0, 0, 0, 0.5, 0.5, 0, 0, 0]);
const imag = new Float32Array([0, 0, 0, 0, 0, 0, 0, 0]);
```

### 8-Bit Sound Functions

| Module method | What it plays |
|---------------|--------------|
| `playReady()` | 3 blunt C3 (130.81 Hz) pulses: at 0s, 0.25s, 0.5s |
| `playSet()` | G3 (196 Hz) pulse + rising square wave 196→392 Hz over 0.4s + engine noise |
| `playGo()` | Ascending arpeggio C4→C6 (7 notes, 50ms each) + engine rev noise |
| `playFinish()` | Descending pentatonic B5→C4 (5 notes, 100ms each) + noise crash |

Each note passes through the pulse wave oscillator and bitcrusher chain. Volume is controlled via the `vol` parameter to `play8BitNote()` (default 0.3) and the master `bitcrusherGainNode` (×3.0).

## Race Sound Events Summary

| Event | Trigger | Module Call |
|-------|---------|-------------|
| Countdown ready | `countdown_phase` (phase=ready) | `FranklinAudio.playReady()` |
| Set | `countdown_phase` (phase=set) | `FranklinAudio.playSet()` |
| Go | `countdown_phase` (phase=go) | `FranklinAudio.playGo()` |
| Finish | `status` (end/reset) or `race_control` (end_race/reset_race) | `FranklinAudio.playFinish()` |

## Implementation Notes

- All audio is generated programmatically via `AudioContext`, `OscillatorNode`, `GainNode`, and `BufferSourceNode`.
- The bitcrusher is lazily initialized on first 8-bit sound play.
- Every sound function is wrapped in `try {} catch (_) {}` to prevent audio errors from breaking the UI.
- Every sound function checks `soundEnabled` and `audioCtx.state !== "suspended"` before playing.
- The web apps broadcast all Redis messages they receive — the client is responsible for filtering and dispatching.
- For the timing algorithm to work correctly, `countdownReadyAt` and `countdownReadyAtMs` must be set atomically when the `ready` phase is received.
- Sound state (`soundEnabled`) is in-memory only; sound style is persisted to `localStorage`.
