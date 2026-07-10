# AGENTS.md

1. Do everything we can through `devbox` tasks.
2. If we need to do something repeatedly, ask whether we should make a `devbox` task for it.
3. A feature is not considered complete until we run linters and fix any resulting errors or warnings.
4. `docs/redis-message-reference.md` is the canonical source for Redis channels/messages and pub/sub ownership; when Redis contracts change, update that file first and have other docs reference it.
5. `franklin-gui.py` uses GTK4; ensure all GTK calls use GTK4 APIs and patterns.
6. When you discover important information about this codebase (new patterns, gotchas, architectural changes, new commands, etc.), update `~/.agents/skills/franklin/SKILL.md` so the shared skill stays current. This is how the project's knowledge grows over time.
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

