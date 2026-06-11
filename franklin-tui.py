import argparse
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import redis
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Digits,
    Footer,
    Header,
    Input,
    Label,
    SelectionList,
    Static,
    TabbedContent,
    TabPane,
)

from gui_config import load_initial_config, write_config
from race.race_contestants import RaceContestants
from race.race_mode import RaceMode
from race.race_snapshot import RaceSnapshot, SnapshotLap, idle_snapshot
from race.race_state import RaceEndMode
from racer_colors import RacerColorScheme, assign_random_scheme
from redis_commands import build_command_envelope, parse_command_envelope


def format_time_cs(seconds_value: float | None) -> str:
    if seconds_value is None or seconds_value == float("inf"):
        return "00:00:00"

    total_cs = max(0, int(seconds_value * 100))
    minutes = min(99, total_cs // 6000)
    seconds = (total_cs // 100) % 60
    centiseconds = total_cs % 100
    return f"{minutes:02}:{seconds:02}:{centiseconds:02}"


class LapDataDisplay(Static):
    laps: reactive[list[Any]] = reactive([])  # type: ignore[valid-type]

    def __init__(self, contestants: RaceContestants, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.contestants: RaceContestants = contestants

    def render(self) -> str:
        if not self.laps:
            return "No lap data yet."
        lines = ["Lap Events:"]
        for lap in self.laps:
            display_name = self.contestants.get_contestant_name(lap.racer_id)
            # Replace racer ID with contestant name if available
            if lap.lap_number == 0:
                lines.append(
                    f"Racer {display_name} START TRIGGER | Time: {format_time_cs(lap.race_time)}"
                )
            else:
                lines.append(
                    f"Racer {display_name} Lap {lap.lap_number} | Race Time: {format_time_cs(lap.race_time)}, Lap Time: {format_time_cs(lap.lap_time)}"
                )
        return "\n".join(lines)

    def refresh_display(self) -> None:
        """Force a refresh of the display to update driver names."""
        self.update()


class RaceStatusDisplay(Static):
    BORDER_TITLE = "Race Status"
    # Snapshot state string (see race.race_snapshot): not_started/running/
    # paused/winner_declared/finished.
    race_state: reactive[str] = reactive("not_started")  # type: ignore[valid-type]
    leader_laps_remaining: reactive[int] = reactive(10)  # type: ignore[valid-type]
    last_place_laps_remaining: reactive[int] = reactive(10)  # type: ignore[valid-type]
    effective_total_laps: reactive[int] = reactive(10)  # type: ignore[valid-type]

    def render(self) -> str:
        status = []
        if self.race_state == "running":
            leader_lap = max(0, self.effective_total_laps - self.leader_laps_remaining)
            status.append("Race in progress")
            status.append("(Lap 0 = Race Start Trigger)")
            status.append(f"Lap {leader_lap} of {self.effective_total_laps}")
            status.append(f"Leader: {self.leader_laps_remaining} laps remaining")
            status.append(
                f"Last Place: {self.last_place_laps_remaining} laps remaining"
            )
        elif self.race_state == "paused":
            status.append("Race paused")
        elif self.race_state == "winner_declared":
            status.append("Race won, wrapping up")
        elif self.race_state == "finished":
            status.append("Race finished")
        else:
            status.append("Race not started")
        return "\n".join(status)


class LeaderboardDisplay(DataTable[Any]):  # type: ignore[type-arg]
    leaderboard: reactive[list[Any]] = reactive([])  # type: ignore[valid-type]

    def __init__(self, contestants: RaceContestants, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.contestants: RaceContestants = contestants

    def on_leaderboard_changed(self) -> None:
        self.clear(columns=True)
        self.add_columns(
            "Position",
            "Racer",
            "Lap Count",
            "Best Lap Time",
            "Last Lap Time",
            "Total Time",
        )
        if not self.leaderboard:
            return
        for (
            position,
            racer_id,
            lap_count,
            best_lap_time,
            last_lap_time,
            total_time,
        ) in self.leaderboard:
            display_name = self.contestants.get_contestant_name(racer_id)
            if position == "DQ":
                display_name = f"{display_name} (DQ)"
            best_lap_display = format_time_cs(best_lap_time)
            last_lap_display = format_time_cs(last_lap_time)
            total_time_display = format_time_cs(total_time)

            row = (
                position,
                display_name,
                lap_count,
                best_lap_display,
                last_lap_display,
                total_time_display,
            )
            self.add_row(*row)

    def watch_leaderboard(self, leaderboard) -> None:
        self.on_leaderboard_changed()

    def refresh_display(self) -> None:
        """Force a refresh of the leaderboard to update driver names."""
        self.on_leaderboard_changed()


class RaceTimeDisplay(Digits):
    BORDER_TITLE = "Race Time"
    elapsed_time = reactive(0.0)

    def watch_elapsed_time(self, elapsed_time: float) -> None:
        """Called when the time attribute changes."""
        self.update(format_time_cs(self.elapsed_time))


class Franklin(App[Any]):  # type: ignore[type-arg]
    TITLE = "Franklin Lap Counter"
    SUB_TITLE = "RC Lap Counter - Fake Race Mode"
    # Note: will be overridden dynamically in update_subtitle
    CSS = """
    Screen {
        align: center middle;
    }

    #race_controls {
        padding: 1;
        margin: 1;
        height: 10;
        width: 1fr;
        background: $background;
    }

    RaceTimeDisplay {
        padding: 1;
        margin: 1;
        background: $background;
        color: $foreground;
        width: 1fr;
    }

    RaceStatusDisplay {
        padding: 1;
        margin: 1;
        background: $surface;
        color: $foreground;
        width: 1fr;
        content-align: center middle;
        border: $secondary tall;
    }

    #tabbed_content {
        height: 1fr;
        padding: 1 2;
    }

    LeaderboardDisplay {
        color: $text-primary;
    }

    """

    BINDINGS = [
        Binding("t", "toggle_mode", "Toggle Mode"),
        Binding("s", "start_race", "Start Race"),
        Binding("e", "end_race", "End Race"),
        Binding("r", "rename_driver", "Rename Driver"),
    ]

    def __init__(
        self,
        *,
        initial_mode: RaceMode,
        total_laps: int,
        contestants_data: list[dict[str, Any]],
        race_end_mode: RaceEndMode,
        last_race_contestant_ids: list[int],
        racer_color_assignments: dict[int, RacerColorScheme],
        redis_socket: str = "./redis.sock",
        **kwargs,
    ):
        super().__init__(**kwargs)

        self.total_laps = total_laps
        self.race_mode = initial_mode
        self.race_end_mode = race_end_mode
        self.global_contestants = RaceContestants(contestants_data)
        self.racer_color_assignments = dict(racer_color_assignments)
        self.last_race_contestant_ids: set[int] = set(last_race_contestant_ids)

        # Authoritative render state from the recorder's franklin:race_state.
        # The TUI never mutates race state; it renders this and publishes
        # commands (see docs/redis-message-reference.md).
        self.snapshot: RaceSnapshot = idle_snapshot()

        known_racer_ids = {
            c.transmitter_id for c in self.global_contestants.contestants
        }.union(last_race_contestant_ids)
        self._ensure_racer_color_assignments(known_racer_ids, persist=False)

        self.lap_counter_detected = reactive(False)
        self._last_lap_counter_signal_time = None

        # Setup logging
        logging.basicConfig(
            filename="race.log",
            filemode="a",
            format="%(asctime)s %(levelname)s:%(message)s",
            level=logging.INFO,
            force=True,
        )
        logging.info("Franklin initialized")
        logging.info(f"Franklin initialized: {self.race_mode}")

        # Redis communication setup (see docs/redis-message-reference.md)
        self.redis_socket = redis_socket
        self.redis_in_channel = "hardware:in"
        self.redis_out_channel = "hardware:out"
        self.redis_events_channel = "franklin:events"
        self.redis_race_state_channel = "franklin:race_state"
        self.redis_race_state_latest_key = "franklin:race_state:latest"
        self._redis_client = None
        self._redis_pubsub = None
        self.config_path = Path("franklin.config.json")
        self.update_subtitle()

    def update_subtitle(self) -> None:
        mode_str = f"RC Lap Counter - {self.race_mode}"
        self.sub_title = mode_str
        try:
            header = self.query_one(Header)
            header.refresh()
        except Exception:
            # Header widget not found yet
            pass

    def _ensure_racer_color_assignments(
        self, racer_ids: set[int], *, persist: bool
    ) -> None:
        changed = False
        for racer_id in racer_ids:
            if racer_id <= 0:
                continue
            if racer_id in self.racer_color_assignments:
                continue
            self.racer_color_assignments[racer_id] = assign_random_scheme(
                self.racer_color_assignments
            )
            changed = True

        if changed and persist:
            self.save_config()

    def save_config(self) -> None:
        contestants = [
            {"transmitter_id": c.transmitter_id, "name": c.name}
            for c in self.global_contestants.contestants
        ]

        last_race_contestant_ids = sorted(self.last_race_contestant_ids)

        write_config(
            self.config_path,
            race_mode=self.race_mode,
            total_laps=self.total_laps,
            race_end_mode=self.race_end_mode,
            contestants_data=contestants,
            last_race_contestant_ids=last_race_contestant_ids,
            racer_color_assignments=self.racer_color_assignments,
        )

    def action_toggle_mode(self) -> None:
        if self.snapshot.is_going:
            logging.info("Cannot change race mode while race is running")
            self.notify(
                "Cannot change race mode while race is running", severity="error"
            )
            return

        # Cycle through modes: FAKE -> REAL -> TRAINING -> FAKE
        if self.race_mode == RaceMode.FAKE:
            self.race_mode = RaceMode.REAL
        elif self.race_mode == RaceMode.REAL:
            self.race_mode = RaceMode.TRAINING
        else:
            self.race_mode = RaceMode.FAKE
        logging.info(f"Toggled race mode to: {self.race_mode}")
        self.update_subtitle()
        self.save_config()

    def _referee_adjusted_leaderboard_data(self) -> list[tuple[Any, ...]]:
        """Display rows from the authoritative snapshot.

        The recorder already applies penalties/DQ and orders rows. ``inf`` keeps
        missing best/last times rendering as ``00:00:00`` via ``format_time_cs``.
        """
        rows: list[tuple[Any, ...]] = []
        for row in self.snapshot.leaderboard:
            position: Any = "DQ" if row.disqualified else row.position
            best = row.best_lap_time if row.best_lap_time is not None else float("inf")
            last = row.last_lap_time if row.last_lap_time is not None else float("inf")
            rows.append(
                (
                    position,
                    row.racer_id,
                    row.lap_count,
                    best,
                    last,
                    row.adjusted_total_time,
                )
            )
        return rows

    async def hardware_monitor_task(self):
        """
        Subscribe to Redis pub/sub to receive hardware and race-control messages.

        Contract reference: docs/redis-message-reference.md
        """
        logging.info("Hardware monitor task starting up")

        # Connect to Redis
        try:
            self._redis_client = redis.Redis(
                unix_socket_path=self.redis_socket, decode_responses=True
            )
            self._redis_client.ping()
            logging.info("Connected to Redis")
        except Exception as e:
            logging.error(f"Failed to connect to Redis: {e}")
            self.notify(f"Failed to connect to Redis: {e}", severity="error")
            return

        # Create pub/sub instance
        self._redis_pubsub = self._redis_client.pubsub()
        channels = (
            self.redis_out_channel,
            self.redis_events_channel,
            self.redis_race_state_channel,
        )
        self._redis_pubsub.subscribe(*channels)
        logging.info("Subscribed to Redis channels: %s", ", ".join(channels))

        self.lap_counter_detected = False
        self._last_lap_counter_signal_time = None

        # Render the retained snapshot so a freshly-started TUI shows current
        # state without waiting for the next publish.
        self._load_latest_snapshot()

        try:
            while True:
                # Get messages from Redis (non-blocking with timeout)
                message = self._redis_pubsub.get_message(timeout=0.1)

                if message and message["type"] == "message":
                    try:
                        channel = message.get("channel")
                        data = message["data"]
                        msg: dict[str, Any] = (
                            json.loads(data) if isinstance(data, (str, bytes)) else {}
                        )

                        if channel == self.redis_race_state_channel:
                            self.handle_snapshot(msg)
                        else:
                            self._handle_hardware_message(msg)
                    except json.JSONDecodeError as e:
                        logging.error(f"Failed to parse Redis message: {e}")

                # Small sleep to prevent busy-waiting
                await asyncio.sleep(0.01)

        except asyncio.CancelledError:
            logging.info("Hardware monitor task cancelled")

        finally:
            # Cleanup Redis connection
            if self._redis_pubsub:
                self._redis_pubsub.unsubscribe()
                self._redis_pubsub.close()
            if self._redis_client:
                self._redis_client.close()
            logging.info("Redis connections closed")

    def _load_latest_snapshot(self) -> None:
        """Fetch the retained snapshot so late joiners render current state."""
        if not self._redis_client:
            return
        try:
            payload = self._redis_client.get(self.redis_race_state_latest_key)
        except Exception as exc:
            logging.error("Failed to read latest snapshot: %s", exc)
            return
        if not isinstance(payload, str):
            return
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            logging.error("Retained snapshot is not valid JSON")
            return
        if isinstance(data, dict):
            self.handle_snapshot(data)

    def handle_snapshot(self, data: dict[str, Any]) -> None:
        """Apply an authoritative race-state snapshot from the recorder."""
        try:
            snapshot = RaceSnapshot.from_dict(data)
        except Exception as exc:
            logging.error("Invalid race snapshot: %s", exc)
            return

        if not snapshot.supersedes(self.snapshot):
            return

        self.snapshot = snapshot

        racer_ids = {row.racer_id for row in snapshot.leaderboard}
        if not racer_ids:
            racer_ids = {lap.racer_id for lap in snapshot.laps}
        if racer_ids:
            self.last_race_contestant_ids = set(racer_ids)
            self._ensure_racer_color_assignments(racer_ids, persist=True)

    def _handle_hardware_message(self, msg: dict[str, Any]) -> None:
        """Display-only handling of hardware/event traffic.

        The recorder owns the race model; here we only surface heartbeat,
        countdown, status and accepted commands to the operator.
        """
        msg_type = msg.get("type")
        simulated = bool(msg.get("simulated", False))

        if msg_type == "heartbeat":
            if not self.lap_counter_detected:
                logging.info("Lap counter detected (heartbeat)")
            self.lap_counter_detected = True
            self._last_lap_counter_signal_time = asyncio.get_event_loop().time()

        elif msg_type == "countdown_phase":
            phase = str(msg.get("phase", "")).lower()
            at_raw = msg.get("at")
            if isinstance(at_raw, (int, float)):
                delay = max(0.0, float(at_raw) - time.time())
                self.set_timer(
                    delay,
                    lambda phase=phase: self.notify(
                        f"Countdown: {phase.title()}", severity="information"
                    ),
                )
            else:
                self.notify(f"Countdown: {phase.title()}", severity="information")

        elif msg_type == "start_race":
            at_raw = msg.get("at")
            if isinstance(at_raw, (int, float)):
                delay = max(0.0, float(at_raw) - time.time())
                self.set_timer(
                    delay,
                    lambda: self.notify("Race started", severity="information"),
                )
            else:
                self.notify("Race started", severity="information")

        elif msg_type == "lap":
            logging.info(
                "%s lap message received: %s",
                "Simulated" if simulated else "Hardware",
                msg,
            )

        elif msg_type == "status":
            logging.info(
                "%s status message: %s",
                "Simulated" if simulated else "Hardware",
                msg.get("message", ""),
            )

        elif msg_type == "race_control":
            logging.info(
                "Race control event: command=%s accepted=%s racer_id=%s message=%s",
                msg.get("command"),
                bool(msg.get("accepted", True)),
                msg.get("racer_id"),
                msg.get("message", ""),
            )

        elif msg_type == "raw":
            logging.debug(f"Raw message: {msg.get('line', '')}")

        else:
            logging.debug(f"Unknown message type: {msg}")

    async def refresh_lap_data(self):
        """Render the authoritative snapshot into the widgets on a timer."""
        lap_display_events = self.query_one(LapDataDisplay)
        lap_display_leaderboard = self.query_one(LeaderboardDisplay)
        race_time_display = self.query_one(RaceTimeDisplay)
        race_status_display = self.query_one(RaceStatusDisplay)
        start_btn = self.query_one("#start_btn", Button)
        stop_btn = self.query_one("#stop_btn", Button)
        while True:
            snapshot = self.snapshot
            race_time_display.elapsed_time = snapshot.current_elapsed()
            lap_display_events.laps = list(snapshot.laps)
            lap_display_leaderboard.leaderboard = (
                self._referee_adjusted_leaderboard_data()
            )
            race_status_display.leader_laps_remaining = snapshot.laps_remaining_leader
            race_status_display.last_place_laps_remaining = snapshot.laps_remaining_last
            race_status_display.effective_total_laps = snapshot.effective_total_laps
            race_status_display.race_state = snapshot.state

            running = snapshot.is_going
            start_btn.disabled = running
            stop_btn.disabled = not running

            await asyncio.sleep(0.1)

    def compose(self) -> ComposeResult:
        # Use RaceContestants instance for contestant data
        yield Header()
        with Vertical():
            with Horizontal():
                with Vertical(id="race_controls"):
                    yield Button("Start Race", id="start_btn")
                    yield Button("End Race", id="stop_btn", disabled=True)
                yield RaceTimeDisplay(name="Race Time", id="race_time", classes="box")
                yield RaceStatusDisplay(id="race_status", classes="box")
            with TabbedContent(id="tabbed_content"):
                with TabPane("Leaderboard", id="leaderboard_tab"):
                    yield LeaderboardDisplay(
                        id="leaderboard",
                        contestants=self.global_contestants,
                    )
                with TabPane("Events", id="events_tab"):
                    yield LapDataDisplay(
                        id="lap_data",
                        contestants=self.global_contestants,
                    )
        yield Footer()

    def _publish_command(self, command: str, **kwargs: Any) -> bool:
        """Publish a race-control command; the recorder owns the effect."""
        if not self._redis_client:
            self.notify("Redis not connected", severity="error")
            return False
        try:
            cmd = build_command_envelope(command, source="franklin_tui", **kwargs)
            validated = parse_command_envelope(cmd)
            self._redis_client.publish(self.redis_in_channel, json.dumps(validated))
            logging.info("Sent %s command: %s", command, validated)
            return True
        except Exception as e:
            logging.error("Failed to send %s command: %s", command, e)
            self.notify(f"Failed to send {command}: {e}", severity="error")
            return False

    def action_start_race(self) -> None:
        if self.snapshot.is_going:
            return

        base = time.time() + 0.25
        ready_at = base
        set_at = base + 1.0
        go_at = base + 2.0

        # Publishes a start_race command (with config) for the recorder; the
        # authoritative race appears via the snapshot.
        if self._publish_command(
            "start_race",
            ready_at=ready_at,
            set_at=set_at,
            go_at=go_at,
            start_at=go_at,
            # Race config for the headless recorder (ignored by the Rust owner).
            race_mode=self.race_mode.value,
            total_laps=self.total_laps,
            race_end_mode=self.race_end_mode.value,
        ):
            self.query_one("#start_btn", Button).disabled = True
            self.notify("Start countdown scheduled", severity="information")

    def action_end_race(self) -> None:
        if not self.snapshot.is_going:
            return
        if self._publish_command("end_race"):
            self.notify("Requested race end", severity="information")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "start_btn":
            self.action_start_race()
        elif button_id == "stop_btn":
            self.action_end_race()

    def action_rename_driver(self) -> None:
        """Action to open the rename driver dialog."""
        self.push_screen(
            RenameDriverScreen(self.global_contestants, self.snapshot.laps),
            self._handle_driver_rename_result,
        )

    def _handle_driver_rename_result(self, result: bool | None) -> None:
        if not result:
            return

        known_racer_ids = {
            c.transmitter_id for c in self.global_contestants.contestants
        }
        self._ensure_racer_color_assignments(known_racer_ids, persist=False)
        self.save_config()
        self.refresh_driver_data()

    def refresh_driver_data(self) -> None:
        """Refresh displays that show driver information."""
        # Update any UI components that display driver names
        self.query_one(LeaderboardDisplay).refresh_display()

        # If we have any lap data displays, update them
        lap_displays = self.query(LapDataDisplay)
        for display in lap_displays:
            # Force a re-render by setting laps to itself
            display.refresh_display()

    async def on_mount(self) -> None:
        asyncio.create_task(self.refresh_lap_data())
        asyncio.create_task(self.hardware_monitor_task())


class RenameDriverScreen(ModalScreen[bool]):
    """
    A modal screen to rename drivers.
    Shows a list of all drivers, with unknown IDs from current race at the top.
    """

    BINDINGS = [
        Binding("escape", "dismiss(False)", "Cancel"),
        Binding("enter", "action_save", "Save", key_display="Enter"),
    ]

    CSS = """
    #rename-driver-modal {
        width: 60%;
        height: 80%;
        background: $panel;
        border: solid $accent;
        padding: 1 2;
    }

    #rename-driver-title {
        text-align: center;
        text-style: bold;
        margin-bottom: 1;
    }

    #driver-list {
        height: 1fr;
        margin-bottom: 1;
    }

    #driver-name-label {
        margin-top: 1;
        margin-bottom: 1;
    }

    #driver-name-input {
        margin-bottom: 1;
    }

    #driver-rename-buttons {
        height: auto;
        width: 100%;
        align: center middle;
        margin-top: 1;
    }

    Button {
        margin: 0 1;
    }
    """

    def __init__(self, contestants, laps: list[SnapshotLap]):
        super().__init__()
        self.contestants = contestants
        self.laps = laps
        self.selected_driver_id = None
        self.driver_name_input = None

    def compose(self) -> ComposeResult:
        """Create UI elements for the screen."""
        with Vertical(id="rename-driver-modal"):
            yield Label("Rename Driver", id="rename-driver-title")
            yield Label("Select a driver to rename:")
            yield SelectionList(id="driver-list")
            yield Label("New name:", id="driver-name-label")
            yield Input(placeholder="Enter driver name", id="driver-name-input")
            with Horizontal(id="driver-rename-buttons"):
                yield Button("Cancel", variant="error", id="cancel-btn")
                yield Button("Save", variant="success", id="save-btn")

    def on_mount(self):
        """
        Set up the driver list when the screen is mounted.
        Unknown driver IDs from the current race will be at the top.
        """
        driver_list = self.query_one("#driver-list", SelectionList)
        driver_list.clear_options()

        # Track which IDs have been seen in the race but don't have names yet
        unknown_ids = set()
        for lap in self.laps:
            racer_id = lap.racer_id
            name = self.contestants.get_contestant_name(racer_id)
            if name.startswith("Unknown"):
                unknown_ids.add(racer_id)

        # Add unknown drivers first
        for transmitter_id in unknown_ids:
            driver_list.add_option(
                (f"⚠️  ID: {transmitter_id} (Unknown)", transmitter_id)
            )

        # Add known drivers
        for contestant in self.contestants.contestants:
            # Skip if already added to unknown list
            if contestant.transmitter_id in unknown_ids:
                continue
            driver_list.add_option((contestant.name, contestant.transmitter_id))

        # Disable input until a driver is selected
        self.query_one("#driver-name-input", Input).disabled = True

    @on(SelectionList.SelectedChanged)
    def handle_selection_change(self, event: SelectionList.SelectedChanged):
        """Handle when a driver is selected from the list."""
        selection = event.selection_list.selected  # Get the selected values
        if not selection:
            self.selected_driver_id = None
            self.query_one("#driver-name-input", Input).disabled = True
            return

        self.selected_driver_id = selection[0]  # Get the first selected value
        input_field = self.query_one("#driver-name-input", Input)
        input_field.disabled = False

        # Fill in existing name if it exists
        current_name = self.contestants.get_contestant_name(self.selected_driver_id)
        if not current_name.startswith("Unknown"):
            input_field.value = current_name
        else:
            input_field.value = ""
            input_field.placeholder = (
                f"Enter name for driver ID {self.selected_driver_id}"
            )

        input_field.focus()

    @on(Button.Pressed, "#cancel-btn")
    def handle_cancel(self):
        """Handle the cancel button press."""
        self.dismiss(False)

    @on(Button.Pressed, "#save-btn")
    def handle_save(self):
        """Handle the save button press, updating the contestant name."""
        self.action_save()

    @on(Input.Submitted, "#driver-name-input")
    def handle_input_submitted(self):
        """Handle the input submitted event (Enter key pressed)."""
        self.action_save()

    def action_save(self):
        """Save action that can be triggered by button or keyboard shortcut."""
        if self.selected_driver_id is None:
            return

        new_name = self.query_one("#driver-name-input", Input).value
        if not new_name:
            return

        # Look for existing contestant with this ID
        existing_contestant = None
        for contestant in self.contestants.contestants:
            if contestant.transmitter_id == self.selected_driver_id:
                contestant.name = new_name
                existing_contestant = contestant
                break

        # If no existing contestant, add a new one
        if existing_contestant is None:
            from race.contestant import Contestant

            new_contestant = Contestant(
                transmitter_id=self.selected_driver_id, name=new_name
            )
            self.contestants.contestants.append(new_contestant)

        self.dismiss(True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Start Franklin Lap Counter in chosen initial mode."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--race", action="store_true", help="Start in Race Mode (Real Race Mode)"
    )
    group.add_argument("--fake", action="store_true", help="Start in Fake Race Mode")
    group.add_argument("--training", action="store_true", help="Start in Training Mode")
    args = parser.parse_args()

    # Determine initial mode based on args
    selected_modes = sum([args.race, args.fake, args.training])
    if selected_modes > 1:
        raise Exception("Only one of --race, --fake, or --training can be specified")

    if args.race:
        initial_mode = RaceMode.REAL
    elif args.fake:
        initial_mode = RaceMode.FAKE
    elif args.training:
        initial_mode = RaceMode.TRAINING
    else:
        initial_mode = RaceMode.TRAINING

    (
        configured_mode,
        total_laps,
        race_end_mode,
        contestants_data,
        last_race_contestant_ids,
        racer_color_assignments,
    ) = load_initial_config(Path("franklin.config.json"))

    app = Franklin(
        initial_mode=initial_mode if selected_modes == 1 else configured_mode,
        total_laps=total_laps,
        contestants_data=contestants_data,
        race_end_mode=race_end_mode,
        last_race_contestant_ids=last_race_contestant_ids,
        racer_color_assignments=racer_color_assignments,
    )
    app.run()
