# AGENTS.md

1. Do everything we can through `devbox` tasks.
2. If we need to do something repeatedly, ask whether we should make a `devbox` task for it.
3. A feature is not considered complete until we run linters and fix any resulting errors or warnings.
4. `docs/redis-message-reference.md` is the canonical source for Redis channels/messages and pub/sub ownership; when Redis contracts change, update that file first and have other docs reference it.
5. `franklin-gui.py` uses GTK4; ensure all GTK calls use GTK4 APIs and patterns.
6. When you discover important information about this codebase (new patterns, gotchas, architectural changes, new commands, etc.), update `AGENTS.md` so the shared knowledge stays current. This is how the project's knowledge grows over time.
7. **Deploy commands to push fixes to the Pi:**
   - `devbox run deploy` — builds `.deb` then runs `ansible:deploy`
   - `devbox run ansible:deploy` — pushes files + `.deb` to Pi
8. **Before running any deploy, restart, or diagnose on the Pi**, confirm with the user in that session that they want live testing. Do not push changes to the Pi without explicit confirmation.

9. **Debugging the GUI / race on the Pi without a display or audio output:**
   - You can drive a race headlessly by publishing command envelopes to the
     recorder's input channel (currently `hardware:in`; check
     `self.redis_in_channel` in `franklin-gui.py`). Use the
     `build_command_envelope` shape:
     `{"type":"command","command":<name>,"command_id":<uuid>,"source":<str>,"timestamp":<iso>,"...fields"}`.
     Redis is reached on the Pi via the unix socket
     `/opt/franklin-lap-counter/run/redis.sock` (owned by `franklin` — run as the
     `franklin` user via `ansible ... -b --become-user=franklin`, NOT as
     `dadisc01`, or you'll get "Permission denied" on the socket).
   - **FAKE race (no hardware needed):** publish `start_race` with
     `race_mode:"Fake Race Mode"`, `ready_at`/`set_at`/`go_at`/`start_at`
     (epoch seconds, e.g. `now+2/+3/+4`), `total_laps`, `race_end_mode`. The
     recorder publishes `countdown_phase` events on `franklin:events` for FAKE
     races only (for REAL/TRAINING the Rust hardware monitor owns the countdown,
     so the same `start_race` command works if the hardware is attached). Publish
     `end_race` / `reset_race` the same way to finish or return to idle.
   - **Verify sounds:** `franklin-gui.py` plays each `countdown_phase`
     (ready/set/go) and the finish transition by running `aplay -q` in a
     background thread. Poll `pgrep -af aplay` on the Pi during a race — each
     beep should spawn one `aplay -q` process. GUI logs go to
     `/opt/franklin-lap-counter/gui.log` (level INFO; the session wrapper also
     copies stderr to `/var/log/franklin/franklin-gui.log`).
   - **Restart the GUI display session after deploying GUI changes:**
     `devbox run ansible-playbook -i playbooks/inventory.ini playbooks/59-restart-franklin-gui.yml`
     (push files first with `devbox run deploy` / `devbox run ansible:deploy-gui`).
   - **Gotcha — countdown lights clobbered to red:** `handle_snapshot` used to
     clear the start-light countdown on *any* `not_started` snapshot. During a
     countdown the race is still `not_started` until `go`, so each `not_started`
     snapshot wiped the lights to red mid-countdown. The sounds kept playing
     (they run in a thread), so they stayed in sync with the web pages while the
     lights flashed red — a confusing symptom. Fix: a `not_started` snapshot must
     not clobber the lights while `_start_sequence_running` is True. When
     debugging light/state-sync issues, add a temporary `logging.info` in the
     `countdown_phase` handler and in the `not_started` branch of
     `handle_snapshot`, run a FAKE race, and grep `gui.log` for those lines;
     remove the temp logs before finishing.

## Architectural Patterns

### Service Grouping (systemd)
All Franklin services are grouped under `franklin.target`. To ensure that restarting the target correctly restarts all member services, each service file must include:
- `PartOf=franklin.target` in the `[Unit]` section.
- `Wants=` or `Requires=` in the `franklin.target` file.

### Hardware Monitor (Rust)
The `franklin-hardware-monitor` is a TUI application. To run it as a background systemd service, it must be launched with the `--headless` flag. This skips terminal initialization and prevents crashes when no TTY is available.

### Redis Connectivity
The project uses a custom Redis instance listening on a Unix socket at `/opt/franklin-lap-counter/run/redis.sock`. 
- Services should use the `FRANKLIN_REDIS_SOCKET` environment variable to locate the socket.
- The default `redis-server.service` (TCP 6379) should be disabled on the Pi to avoid confusion.

## GUI / Start Sequence

### Start Light Pattern
The start lights (mirrored on both sides of the timer) indicate the race start sequence and current race state:

**Start Sequence (Countdown phases):**
- **Ready:** The outermost lights (farthest from the timer) turn RED, all other lights off.
- **Set:** The middle lights turn on YELLOW (added to the RED lights).
- **Go:** The innermost lights (closest to the timer) turn on GREEN (all three lights are now on).

**Global Race States (when no countdown is running):**
- **Race Running:** All lights GREEN.
- **Race Paused:** All lights YELLOW.
- **Race Ready to Start (not_started):** All lights RED.
- **Race Finished:** All lights OFF (gray).

**History:** Previously used a "symmetrically outward green fill" pattern which was replaced by this Red-Yellow-Green sequence.

### Countdown Ownership
- **FAKE mode:** The `franklin-race-recorder.py` publishes `countdown_phase` events.
- **REAL/TRAINING mode:** The Rust `franklin-hardware-monitor` owns the countdown and publishes events.
- **GUI Fallback:** The GUI implements a local visual/audio fallback countdown in case Redis events are delayed or lost. It stands down as soon as a real `countdown_phase` event is received (`_countdown_event_seen`).

### Display Resolution & Reboot Blanking
- The monitor resolution is pinned in two places (both driven by `playbooks/group_vars/all.yml`):
  - `franklin_display_mode` (default `1920x1080@60Hz`) → sway `output * mode` in `playbooks/56-wayland-sway.yml` (sway accepts fractional refresh rates).
  - `franklin_kms_video` (default `HDMI-A-1:1920x1080@60D`) → kernel `video=` parameter in `/boot/firmware/cmdline.txt` via `playbooks/57-hdmi-hotplug.yml` (kernel only accepts integer refresh; `M` = CVT timings, `D` = force the digital output on even without EDID/hotplug — this keeps the monitor from turning off during reboot).
- `57-hdmi-hotplug.yml` also adds `consoleblank=0` to the cmdline (no console blanking) and keeps `hdmi_force_hotplug=1` in `config.txt`; the sway config sets `output * dpms on` so DPMS never blanks the kiosk display.
- Apply with `devbox run ansible-playbook -i playbooks/inventory.ini playbooks/56-wayland-sway.yml` / `57-hdmi-hotplug.yml`; cmdline/firmware changes need a reboot to take effect.
- **Gotcha:** `devbox run deploy` / `ansible:deploy` runs `deploy-franklin.yml` only — it does NOT run the setup playbooks (56/57 etc.), so display config changes must be applied by running those playbooks explicitly.
- **Gotcha — sway version vs DPMS:** Sway 1.7 (on Debian 12/Pi) uses `output * dpms on`. Newer sway versions might use `output * power on`. If you see "Unknown command: power" in sway logs, use `dpms`.
- **Gotcha — Window alignment/overscan:** If the GUI appears shifted or cut off, ensure the resolution matches the monitor's native mode and that `for_window [app_id="com.franklin.lapcounter.gui"] fullscreen enable` is present in the sway config to force the app to fill the output.
- The sway mode can be applied without a reboot: run `scripts/reload_sway_and_report.sh` on the Pi as `franklin` (e.g. `ansible all -i playbooks/inventory.ini -b --become-user=franklin -m script -a scripts/reload_sway_and_report.sh`); it reloads sway and prints the active mode. The kernel `video=` cmdline part only affects boot/console and still needs a reboot.

### Version-based Updates
The Ansible deployment playbook (`playbooks/deploy-franklin.yml`) only re-installs the Rust Debian package if the version number in `Cargo.toml` is strictly greater than the version currently installed on the Pi.
- **Gotcha:** If you make code changes but don't bump the version, your changes will NOT be deployed.
- **Fix:** Always increment the version (e.g., in `rust/Cargo.toml`) before running `devbox run deploy`.

### Cross-Compilation
When deploying from a Mac to the Pi (ARM64), `devbox` uses a `container` service for cross-compilation.
- **Fix:** If you see `docker` or `container` errors during `deploy`, ensure the `container` service is started in your devbox environment.

## Troubleshooting

### Hardware Monitor Logs
- `hardware.log`: General logs from the hardware monitor.
- `hardware_redis.log`: Specific logs related to Redis connectivity and data processing.
- `journalctl -u franklin-hardware-monitor`: Systemd logs (useful for catching startup crashes).

### Caddy Reverse Proxy Logs
- Caddy access logs (every HTTP request) are enabled and sent to the systemd journal.
- **View logs:** `journalctl -u caddy -f`
- **Search logs:** `journalctl -u caddy | grep "handled request"`

### Diagnosing the Pi
Use `devbox run ansible:diagnose` for a full health check of all services, Redis socket, and web app connectivity.

## Logging Strategy

### Race Start Sequence
To observe the race start sequence across components, check the following logs:
- **Recorder:** `race_recorder.log` (logs when it publishes FAKE countdown phases).
- **GUI:** `gui.log` (logs when it receives and applies countdown phases).
- **TUI:** `race.log` (logs when it receives countdown phases).
- **Hardware Monitor (Rust):** `hardware_redis.log` (logs when it publishes REAL/TRAINING countdown phases).
- **Web Apps:** Browser `console.log` or the internal debug log on the Referee page.

Search for "Countdown phase" or "COUNTDOWN" in these logs to verify the Ready-Set-Go sequence.

## Networking

### Wi-Fi Hotspot (Access Point)
When enabled via `franklin_enable_hotspot: true`, the Pi acts as a 2.4GHz access point:
- **SSID:** `FranklinLapCounter` (default)
- **Channel:** `7` (default)
- **IP Address:** `10.210.1.1`
- **DNS:** `scoreboard.frank`, `referee.frank`, `healthcheck.frank`, `racer.frank` all resolve to the Pi.
- **Config:** Managed via `playbooks/45-network-hotspot.yml`.
- **Ethernet Access:** Web services (ports 80, 8081, 8082, 8083, 8085) are allowed on the ethernet (uplink) interface, but DNS resolution is only provided on the Wi-Fi interface.

