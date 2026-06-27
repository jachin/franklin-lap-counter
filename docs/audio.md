# Audio Notes

## Overview

Audio in the Franklin Lap Counter project is **client-side only** — all sound is generated at runtime in the browser using the Web Audio API. No audio files are embedded or shipped with the project.

## Files with Audio

| File | Type | Description |
|------|------|-------------|
| `static/driver.html` | Client-side | Only file with audio. Generates all race sounds via the Web Audio API (oscillators, noise buffers, bitcrusher effects). |

## Sound Styles

The driver page offers two sound styles selectable from the UI:

- **Classic** — Smooth sine/square wave tones with long fades. Used as the reference for timing (e.g., two same tones at 0.9s each, then one higher tone at 2s for the race start).
- **8-bit** — Square wave oscillators with a bitcrusher effect for a retro chiptune feel.

## Race Sound Events

| Event | Function (8-bit) | Function (Classic) |
|-------|-------------------|---------------------|
| Countdown ready | `play8BitReadySound()` | `playReadySound()` |
| Set (green light) | `play8BitSetSound()` | `playSetSound()` |
| Go (race start) | `play8BitGoSound()` | `playGoSound()` |
| Finish | `play8BitFinishSound()` | `playFinishSound()` |

## Timing Reference (8-bit Race Start)

The 8-bit race start sound uses **3 notes** distributed across the countdown phases, matching the classic style:

| Phase | Note | Pitch | Duration |
|-------|------|-------|----------|
| Ready | 1 | 196 Hz | 0.9s |
| Set | 2 | 196 Hz | 0.9s |
| Go | 3 | 392 Hz (one octave up) | 2.0s |

## Implementation Notes

- All audio is generated programmatically via `AudioContext`, `OscillatorNode`, `GainNode`, and `BufferSourceNode`.
- The bitcrusher effect is applied via a `WaveShaperNode` for the 8-bit style.
- Audio is only active when `state.soundEnabled` is `true` and the `AudioContext` is not in a `"suspended"` state.
- The `initBitcrusher()` function lazily creates the bitcrusher node on first use.
