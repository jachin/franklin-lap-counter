import argparse
import asyncio
import json
import logging
import pprint
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

from database import LapDatabase
from race.race import (
    Race,
    RaceState,
    generate_fake_race,
    is_race_going,
    make_fake_lap,
    make_lap_from_sensor_data_and_race,
    order_laps_by_occurrence,
)
from race.race_contestants import RaceContestants
from race.race_mode import RaceMode


class LapDataDisplay(Static):
    laps: reactive[list[Any]] = reactive([])  # type: ignore[valid-type]

    def __init__(self, contestants: RaceContestants, race: Race, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.contestants: RaceContestants = contestants
        self.race: Race = race

    def render(self) -> str:
        if not self.laps:
            return "No lap data yet."
        lines = ["Lap Events:"]
        for lap in self.laps:
            display_name = self.contestants.get_contestant_name(lap.racer_id)
            # Replace racer ID with contestant name if available
            if lap.lap_number == 0:
                lines.append(
                    f"Racer {display_name} START TRIGGER | Time: {lap.seconds_from_race_start:.2f}s"
                )
            else:
                lines.append(
                    f"Racer {display_name} Lap {lap.lap_number} | Hardware: {lap.seconds_from_race_start:.2f}s, Internal: {lap.internal_lap_time:.2f}s, Lap Time: {lap.lap_time:.2f}s"
                )
        return "\n".join(lines)

    def refresh_display(self) -> None:
        """Force a refresh of the display to update driver names."""
        self.update()


class RaceStatusDisplay(Static):
    BORDER_TITLE = "Race Status"
    race_state: reactive[RaceState] = reactive(RaceState.NOT_STARTED)  # type: ignore[valid-type]
    leader_laps_remaining: reactive[int] = reactive(10)  # type: ignore[valid-type]
    last_place_laps_remaining: reactive[int] = reactive(10)  # type: ignore[valid-type]

    def render(self) -> str:
        status = []
        if self.race_state == RaceState.RUNNING:
            status.append("Race in progress")
            status.append("(Lap 0 = Race Start Trigger)")
            status.append(f"Leader: {self.leader_laps_remaining} laps remaining")
            status.append(
                f"Last Place: {self.last_place_laps_remaining} laps remaining"
            )
        elif self.race_state == RaceState.PAUSED:
            status.append("Race paused")
        elif self.race_state == RaceState.WINNER_DECLARED:
            status.append("Race won, wrapping up")
        elif self.race_state == RaceState.FINISHED:
            status.append("Race finished")
        else:
            status.append("Race not started")
        return "\n".join(status)


class LeaderboardDisplay(DataTable[Any]):  # type: ignore[type-arg]
    leaderboard: reactive[list[Any]] = reactive([])  # type: ignore[valid-type]

    def __init__(self, contestants: RaceContestants, race: Race, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.contestants: RaceContestants = contestants
        self.race: Race = race

    def on_leaderboard_changed(self) -> None:
        self.clear(columns=True)
        self.add_columns(
            "Position",
            "Racer",
            "Lap Count",
            "Best Lap Time (s)",
            "Last Lap Time (s)",
            "Total Time (s)",
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
            # Format lap times, showing blank instead of "inf"
            best_lap_display = (
                "" if best_lap_time == float("inf") else f"{best_lap_time:.2f}"
            )
            last_lap_display = (
                "" if last_lap_time == float("inf") else f"{last_lap_time:.2f}"
            )
            total_time_display = (
                "" if total_time == float("inf") else f"{total_time:.2f}"
            )

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
        minutes = int(self.elapsed_time // 60)
        seconds = int(self.elapsed_time % 60)
        tenths = int((self.elapsed_time - seconds) * 10)
        self.update(f"{minutes}:{seconds}:{tenths}")


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
        total_laps,
        contestants_data,
        redis_socket="./redis.sock",
        db_path="lap_counter.db",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.lap_queue = asyncio.Queue()

        self.total_laps = total_laps
        self.race_mode = initial_mode
        self.global_contestants = RaceContestants(contestants_data)
        self.previous_race = None
        self.race = Race(previous_race=None)
        self.race.total_laps = self.total_laps

        self.lap_counter_detected = reactive(False)
        self._last_lap_counter_signal_time = None
        self._playback_task = None

        # Setup logging
        logging.basicConfig(
            filename="race.log",
            filemode="a",
            format="%(asctime)s %(levelname)s:%(message)s",
            level=logging.INFO,
        )
        logging.info("Franklin initialized")
        logging.info(f"Franklin initialized: {self.race_mode}")

        # Database setup
        self.db = LapDatabase(db_path)
        self.current_race_id = None

        # Check for in-progress race and resume if found
        in_progress = self.db.get_in_progress_race()
        if in_progress:
            logging.info(f"Resuming in-progress race: {in_progress['id']}")
            self.current_race_id = in_progress["id"]
            # Load laps from database and restore race state
            self._restore_race_from_db(in_progress["id"])

        # Redis communication setup
        self.redis_socket = redis_socket
        self.redis_in_channel = "hardware:in"
        self.redis_out_channel = "hardware:out"
        self._redis_client = None
        self._redis_pubsub = None
        self.config_path = Path("config.json")
        self.update_subtitle()

    def _restore_race_from_db(self, race_id: int) -> None:
        """Restore race state from database"""
        try:
            laps = self.db.get_race_laps(race_id)
            logging.info(f"Restored {len(laps)} laps from database for race {race_id}")
            # Note: We don't automatically start the race, just load the data
            # The user can decide whether to continue or start fresh
        except Exception as e:
            logging.error(f"Failed to restore race from database: {e}")

    def update_subtitle(self) -> None:
        mode_str = f"RC Lap Counter - {self.race_mode}"
        self.sub_title = mode_str
        try:
            header = self.query_one(Header)
            header.refresh()
        except Exception:
            # Header widget not found yet
            pass

    def action_toggle_mode(self) -> None:
        if self.race.state == RaceState.RUNNING:
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

    async def update_race_time(self):
        # TODO this works for now but we should probably use the time that's coming
        # from the lap counter it self
        while True:
            if (
                self.race.state == RaceState.RUNNING
                and self.race.start_time is not None
            ):
                self.race.elapsed_time = (
                    asyncio.get_event_loop().time() - self.race.start_time
                )
            await asyncio.sleep(0.1)

    async def hardware_monitor_task(self):
        """
        Subscribe to Redis pub/sub to receive hardware messages.
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
        self._redis_pubsub.subscribe(self.redis_out_channel)
        logging.info(f"Subscribed to Redis channel: {self.redis_out_channel}")

        self.lap_counter_detected = False
        self._last_lap_counter_signal_time = None

        try:
            while True:
                # Get messages from Redis (non-blocking with timeout)
                message = self._redis_pubsub.get_message(timeout=0.1)

                if message and message["type"] == "message":
                    try:
                        data = message["data"]
                        msg: dict[str, Any] = (
                            json.loads(data) if isinstance(data, (str, bytes)) else {}
                        )
                        msg_type = msg.get("type")

                        logging.debug(
                            f"Received hardware message of type '{msg_type}': {msg}"
                        )

                        # Rely only on heartbeat message to update detection
                        if msg_type == "heartbeat":
                            if not self.lap_counter_detected:
                                logging.info("Lap counter detected (heartbeat)")
                            self.lap_counter_detected = True
                            self._last_lap_counter_signal_time = (
                                asyncio.get_event_loop().time()
                            )

                        elif msg_type == "lap":
                            logging.info(f"Lap message received: {msg}")
                            if self.race.state == self.race.state.RUNNING:
                                racer_id = msg.get("racer_id")
                                hardware_race_time = msg.get("race_time")
                                if (
                                    racer_id is not None
                                    and hardware_race_time is not None
                                ):
                                    # Capture the internal (monotonic) time from the event loop.
                                    internal_time = asyncio.get_event_loop().time()

                                    lap = make_lap_from_sensor_data_and_race(
                                        racer_id,
                                        hardware_race_time,
                                        internal_time,
                                        self.race,
                                    )
                                    logging.info("new lap %s", pprint.pformat(lap))
                                    await self.lap_queue.put(lap)
                                else:
                                    logging.error("Invalid lap data received")
                            else:
                                logging.error("Cannot add lap - race is not running")

                        elif msg_type == "new_msg":
                            # Handle your new message type here
                            logging.info(f"New message received: {msg}")

                        elif msg_type == "status":
                            logging.info(f"Status message: {msg.get('message', '')}")

                        elif msg_type == "raw":
                            logging.debug(f"Raw message: {msg.get('line', '')}")

                        else:
                            logging.debug(f"Unknown message type: {msg}")
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

    async def refresh_lap_data(self):
        lap_display_events = self.query_one(LapDataDisplay)
        lap_display_leaderboard = self.query_one(LeaderboardDisplay)
        race_time_display = self.query_one(RaceTimeDisplay)
        race_status_display = self.query_one(RaceStatusDisplay)
        while True:
            race_time_display.elapsed_time = self.race.elapsed_time
            try:
                lap = await asyncio.wait_for(self.lap_queue.get(), timeout=0.1)
                logging.info("adding lap: %s", self.race.state)
                self.race.add_lap(lap)

                # Save lap to database
                if self.current_race_id:
                    try:
                        self.db.add_lap(
                            race_id=self.current_race_id,
                            racer_id=lap.racer_id,
                            sensor_id=getattr(
                                lap, "sensor_id", lap.racer_id
                            ),  # Use racer_id as fallback
                            race_time=lap.seconds_from_race_start,
                            lap_number=lap.lap_number,
                            lap_time=lap.lap_time if lap.lap_number > 0 else None,
                        )
                        logging.debug(
                            f"Saved lap to database: racer={lap.racer_id}, lap={lap.lap_number}"
                        )
                    except Exception as e:
                        logging.error(f"Failed to save lap to database: {e}")

                lap_display_events.laps = self.race.laps.copy()
                lap_display_leaderboard.leaderboard = self.race.leaderboard()
                leader_remaining, last_remaining = self.race.laps_remaining()
                race_status_display.leader_laps_remaining = leader_remaining
                race_status_display.last_place_laps_remaining = last_remaining
                race_status_display.race_state = self.race.state
            except asyncio.TimeoutError:
                # No new lap data, just refresh displays
                lap_display_events.laps = self.race.laps.copy()
                lap_display_leaderboard.leaderboard = self.race.leaderboard()
                leader_remaining, last_remaining = self.race.laps_remaining()
                race_status_display.leader_laps_remaining = leader_remaining
                race_status_display.last_place_laps_remaining = last_remaining
                race_status_display.race_state = self.race.state

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
                        race=self.race,
                    )
                with TabPane("Events", id="events_tab"):
                    yield LapDataDisplay(
                        id="lap_data",
                        contestants=self.global_contestants,
                        race=self.race,
                    )
        yield Footer()

    def action_start_race(self) -> None:
        async def start_race_and_send_command():
            status_display = self.query_one(RaceStatusDisplay)
            start_btn = self.query_one("#start_btn", Button)
            stop_btn = self.query_one("#stop_btn", Button)

            # Create a new race instance with current app settings
            self.race = Race(previous_race=self.previous_race)
            self.race.total_laps = self.total_laps

            if self.race.state != RaceState.RUNNING:
                current_time = asyncio.get_event_loop().time()

                # This call updates the state of the race
                self.race.start(start_time=current_time)

                # Create database record for this race
                self.current_race_id = self.db.create_race(
                    notes=f"Mode: {self.race_mode}, Total Laps: {self.total_laps}"
                )
                logging.info(f"Created database race record: {self.current_race_id}")

                status_display.race_state = self.race.state
                start_btn.disabled = True
                stop_btn.disabled = False

                if (
                    hasattr(self, "_playback_task")
                    and self._playback_task is not None
                    and not self._playback_task.done()
                ):
                    self._playback_task.cancel()

                if self.race_mode == RaceMode.FAKE:
                    # Generate a fake race
                    fake_race = generate_fake_race()
                    logging.info("Starting fake race")
                    logging.info("fake_race %s", fake_race)
                    logging.info("self.race %s", self.race)

                    self.race.state = RaceState.RUNNING

                    # Start playback task
                    self._playback_task = asyncio.create_task(
                        self.play_fake_race(fake_race)
                    )
                else:
                    # Real race mode - send start command via Redis
                    logging.info("Starting real race")
                    try:
                        if self._redis_client:
                            cmd = {"type": "command", "command": "start_race"}
                            self._redis_client.publish(
                                self.redis_in_channel, json.dumps(cmd)
                            )
                            logging.info("Sent start_race command to Redis")
                        else:
                            logging.error("Redis client not initialized")
                            self.notify("Redis not connected", severity="error")
                    except Exception as e:
                        logging.error(f"Failed to send start command to Redis: {e}")
                        self.notify(f"Failed to start race: {e}", severity="error")

        asyncio.create_task(start_race_and_send_command())

    def action_end_race(self) -> None:
        status_display = self.query_one(RaceStatusDisplay)
        start_btn = self.query_one("#start_btn", Button)
        stop_btn = self.query_one("#stop_btn", Button)
        if is_race_going(self.race):
            # Stop playback and reset race state
            if (
                hasattr(self, "_playback_task")
                and self._playback_task is not None
                and not self._playback_task.done()
            ):
                self._playback_task.cancel()
            self.race.state = RaceState.FINISHED
            status_display.race_state = self.race.state
            start_btn.disabled = False
            stop_btn.disabled = True
            # Store this race for the next one
            self.previous_race = self.race

            # Publish end_race command to Redis
            if self._redis_client:
                cmd = {"type": "command", "command": "end_race"}
                self._redis_client.publish(self.redis_in_channel, json.dumps(cmd))
                logging.info("Sent end_race command to Redis")

            # Mark race as completed in database
            if self.current_race_id:
                self.db.end_race(self.current_race_id)
                logging.info(f"Ended database race record: {self.current_race_id}")
                self.current_race_id = None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "start_btn":
            self.action_start_race()
        elif button_id == "stop_btn":
            self.action_end_race()

    def action_rename_driver(self) -> None:
        """Action to open the rename driver dialog."""
        self.push_screen(
            RenameDriverScreen(self.global_contestants, self.race, self.config_path),
            lambda result: self.refresh_driver_data() if result else None,
        )

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
        asyncio.create_task(self.update_race_time())
        asyncio.create_task(self.refresh_lap_data())
        asyncio.create_task(self.hardware_monitor_task())

    async def play_fake_race(self, fake_race):
        """
        Asynchronously plays back the fake race laps in real time based on lap completion times.
        Emits lap events to lap_queue so UI updates as if real.
        """
        if not fake_race.laps:
            logging.error("Fake race has no laps")
            return

        # Verify race is running before proceeding
        if self.race.state != RaceState.RUNNING:
            logging.error("Race not in running state, cannot play fake race")
            return

        start_time = self.race.start_time
        if start_time is None:
            logging.error("Race start time not set")
            return

        sorted_laps = order_laps_by_occurrence(fake_race.laps)
        logging.info("Sorted laps:\n%s", pprint.pformat(sorted_laps))

        try:
            for ts, lap in sorted_laps:
                elapsed_time = asyncio.get_event_loop().time() - start_time
                wait_time = ts - elapsed_time
                if wait_time > 0:
                    await asyncio.sleep(wait_time)

                lap_event = make_fake_lap(
                    lap.racer_id, lap.lap_number, lap.lap_time, ts
                )
                logging.info("fake lap %s", lap_event)
                await self.lap_queue.put(lap_event)
        except asyncio.CancelledError:
            logging.info("Fake race playback cancelled")


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

    def __init__(self, contestants, race, config_path):
        super().__init__()
        self.contestants = contestants
        self.race = race
        self.config_path = config_path
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
        for lap in self.race.laps:
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

        # Save the updated contestants to config.json
        self.save_config()
        self.dismiss(True)

    def save_config(self):
        """Save the updated contestants to config.json."""
        # Check if config file exists
        if not self.config_path.exists():
            logging.warning(
                f"Config file {self.config_path} does not exist, creating it."
            )
            self.notify("Creating new config file", severity="information")

        # Load existing config or create new
        try:
            if self.config_path.exists():
                config_data = json.loads(self.config_path.read_text())
            else:
                config_data = {"total_laps": 10, "contestants": []}
        except Exception as e:
            logging.error(f"Failed to read config.json: {e}")
            config_data = {"total_laps": 10, "contestants": []}

        # Update contestants in config
        contestants_data = []
        for contestant in self.contestants.contestants:
            contestants_data.append(
                {"transmitter_id": contestant.transmitter_id, "name": contestant.name}
            )

        config_data["contestants"] = contestants_data

        # Write back to file
        try:
            self.config_path.write_text(json.dumps(config_data, indent=2))
            logging.info("Updated config.json with new driver names")
            self.notify(
                "Driver information updated successfully", severity="information"
            )
        except Exception as e:
            logging.error(f"Failed to write to config.json: {e}")
            self.notify(f"Error saving configuration: {e}", severity="error")


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

    config_path = Path("config.json")

    total_laps = 10
    contestants_data = []

    if config_path.exists():
        try:
            config_data = json.loads(config_path.read_text())
            total_laps = config_data.get("total_laps", 10)
            contestants_data = config_data.get("contestants", [])
        except Exception as e:
            logging.error(f"Failed to read config.json: {e}")

    app = Franklin(
        initial_mode=initial_mode,
        total_laps=total_laps,
        contestants_data=contestants_data,
    )
    app.run()
