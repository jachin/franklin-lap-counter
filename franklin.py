import argparse
import asyncio
import logging
from textual.app import App, ComposeResult
from textual.containers import Vertical, Horizontal
from textual.widgets import Header, Footer, Static, Button, TabbedContent, TabPane, Digits, DataTable
from textual.reactive import reactive
from race.race import Race, RaceState
from race.race import generate_fake_race, order_laps_by_occurrence, make_lap_from_sensor_data_and_race, make_fake_lap
from textual.binding import Binding
import pprint
from race.race_mode import RaceMode
import multiprocessing
from async_multiprocessing_bridge import AsyncMultiprocessingQueueBridge

class LapDataDisplay(Static):
    laps = reactive([])

    def render(self) -> str:
        if not self.laps:
            return "No lap data yet."
        lines = ["Lap Events:"]
        for lap in self.laps:
            lines.append(str(lap))
        return "\n".join(lines)

class RaceStatusDisplay(Static):
    BORDER_TITLE = "Race Status"
    race_state = reactive(RaceState.NOT_STARTED)
    leader_laps_remaining = reactive(10)
    last_place_laps_remaining = reactive(10)

    def render(self) -> str:
        status = []
        if self.race_state == RaceState.RUNNING:
            status.append("Race in progress")
            status.append("(Lap 0 = Race Start Trigger)")
            status.append(f"Leader: {self.leader_laps_remaining} laps remaining")
            status.append(f"Last Place: {self.last_place_laps_remaining} laps remaining")
        elif self.race_state == RaceState.PAUSED:
            status.append("Race paused")
        elif self.race_state == RaceState.FINISHED:
            status.append("Race finished")
        else:
            status.append("Race not started")
        return "\n".join(status)

class LeaderboardDisplay(DataTable):
    leaderboard = reactive([])

    def on_leaderboard_changed(self) -> None:
        self.clear(columns=True)
        self.add_columns("Position", "Racer ID", "Lap Count", "Best Lap Time (s)", "Total Time (s)")
        if not self.leaderboard:
            return
        for position, racer_id, lap_count, best_lap_time, total_time in self.leaderboard:
            row = (position, racer_id, lap_count, f"{best_lap_time:.2f}", f"{total_time:.2f}")
            self.add_row(*row)

    def watch_leaderboard(self, leaderboard) -> None:
        self.on_leaderboard_changed()

class RaceTimeDisplay(Digits):
    BORDER_TITLE = "Race Time"
    elapsed_time = reactive(0.0)

    def watch_elapsed_time(self, elapsed_time: float) -> None:
        """Called when the time attribute changes."""
        minutes = int(self.elapsed_time // 60)
        seconds = int(self.elapsed_time % 60)
        tenths = int((self.elapsed_time - seconds)*10)
        self.update(f"{minutes}:{seconds}:{tenths}")

class HardwareMonitorGUI(App):
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
        Binding("ctrl+s", "start_race", "Start Race"),
        Binding("ctrl+x", "end_race", "End Race"),
        Binding("ctrl+t", "toggle_mode", "Toggle Race Mode"),
    ]

    def __init__(self, *, initial_mode: RaceMode = RaceMode.TRAINING, **kwargs):
        super().__init__(**kwargs)
        self.lap_queue = asyncio.Queue()
        self.race = Race()
        self.race_mode = initial_mode  # Use provided initial mode or default to training mode
        self.lap_counter_detected = reactive(False)
        self._last_lap_counter_signal_time = None
        self._playback_task = None

        # Setup logging
        logging.basicConfig(
            filename='race.log',
            filemode='a',
            format='%(asctime)s %(levelname)s:%(message)s',
            level=logging.INFO
        )
        logging.info(f"HardwareMonitorGUI initialized: {self.race_mode.value}")

        # Multiprocessing communication setup
        self._hardware_in_queue = multiprocessing.Queue()
        self._hardware_out_queue = multiprocessing.Queue()
        self._hardware_process = None
        self._hardware_async_bridge = None
        self.update_subtitle()

    def update_subtitle(self) -> None:
        mode_str = f"RC Lap Counter - {self.race_mode.value}"
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
            self.notify("Cannot change race mode while race is running", severity="error")
            return

        # Cycle through modes: FAKE -> REAL -> TRAINING -> FAKE
        if self.race_mode == RaceMode.FAKE:
            self.race_mode = RaceMode.REAL
        elif self.race_mode == RaceMode.REAL:
            self.race_mode = RaceMode.TRAINING
        else:
            self.race_mode = RaceMode.FAKE
        logging.info(f"Toggled race mode to: {self.race_mode.value}")
        self.update_subtitle()

    async def update_race_time(self):
        # TODO this works for now but we should probably use the time that's coming
        # from the lap counter it self
        while True:
            if self.race.state == RaceState.RUNNING and self.race.start_time is not None:
                self.race.elapsed_time = asyncio.get_event_loop().time() - self.race.start_time
            await asyncio.sleep(0.1)

    async def hardware_monitor_task(self):
        """
        Async consume hardware process messages via AsyncMultiprocessingQueueBridge,
        handle the structured messages accordingly.
        """
        logging.info("Hardware monitor task starting up")

        # Start hardware process if not already started
        if self._hardware_process is None:
            logging.info("Hardware monitor task has not been started yet, let's start it")
            from hardware_comm_process import start_hardware_comm_process
            self._hardware_process = multiprocessing.Process(
                target=start_hardware_comm_process,
                args=(self._hardware_in_queue, self._hardware_out_queue),
                daemon=True
            )
            self._hardware_process.start()

            logging.info(f"Started hardware_comm_process with PID: {self._hardware_process.pid}")

            # Create async bridge to out_queue
            self._hardware_async_bridge = AsyncMultiprocessingQueueBridge(self._hardware_out_queue, loop=asyncio.get_event_loop())

        logging.info(f"hardware_comm_process running with PID: {self._hardware_process.pid}")

        self.lap_counter_detected = False
        self._last_lap_counter_signal_time = None

        try:
            while True and self._hardware_async_bridge is not None:
                # Get next hardware message async
                msg = await self._hardware_async_bridge.get()

                msg_type = msg.get("type")

                logging.debug(f"Received hardware message of type '{msg_type}': {msg}")

                # Rely only on heartbeat message to update detection
                if msg_type == "heartbeat":
                    if not self.lap_counter_detected:
                        logging.info("Lap counter detected (heartbeat)")
                    self.lap_counter_detected = True
                    self._last_lap_counter_signal_time = asyncio.get_event_loop().time()

                elif msg_type == "lap":
                    logging.info(f"Lap message received: {msg}")
                    if self.race.state == self.race.state.RUNNING:
                        racer_id = msg.get("racer_id")
                        hardware_race_time = msg.get("race_time")
                        if racer_id is not None and hardware_race_time is not None:
                            # Capture the internal (monotonic) time from the event loop.
                            internal_time = asyncio.get_event_loop().time()

                            lap = make_lap_from_sensor_data_and_race(racer_id, hardware_race_time, internal_time, self.race)
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
                    logging.info(f"Status message: {msg.get('message','')}")

                elif msg_type == "raw":
                    logging.debug(f"Raw message: {msg.get('line','')}")

                else:
                    logging.debug(f"Unknown message type: {msg}")

        except asyncio.CancelledError:
            logging.info("Hardware monitor task cancelled")

        finally:
            # Cleanup bridge and process on exit
            if self._hardware_async_bridge:
                self._hardware_async_bridge.stop()
            if self._hardware_process:
                self._hardware_process.terminate()
                self._hardware_process.join()

    async def refresh_lap_data(self):
        lap_display_events = self.query_one(LapDataDisplay)
        lap_display_leaderboard = self.query_one(LeaderboardDisplay)
        race_time_display = self.query_one(RaceTimeDisplay)
        race_status_display = self.query_one(RaceStatusDisplay)
        while True:
            race_time_display.elapsed_time = self.race.elapsed_time
            try:
                lap = await asyncio.wait_for(self.lap_queue.get(), timeout=0.1)
                self.race.add_lap(lap)
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
                    yield LeaderboardDisplay(id="leaderboard")
                with TabPane("Events", id="events_tab"):
                    yield LapDataDisplay(id="lap_data")
        yield Footer()

    def action_start_race(self) -> None:
        async def start_race_and_send_command():
            status_display = self.query_one(RaceStatusDisplay)
            start_btn = self.query_one("#start_btn", Button)
            stop_btn = self.query_one("#stop_btn", Button)

            self.race.reset()

            if self.race.state != RaceState.RUNNING:

                current_time = asyncio.get_event_loop().time()

                # This call updates the state of the race
                self.race.start(start_time=current_time)

                status_display.race_state = self.race.state
                start_btn.disabled = True
                stop_btn.disabled = False

                if hasattr(self, "_playback_task") and self._playback_task is not None and not self._playback_task.done():
                    self._playback_task.cancel()

                if self.race_mode == RaceMode.FAKE:
                    # Generate a fake race
                    fake_race = generate_fake_race()
                    logging.info("Starting fake race")
                    logging.info("fake_race %s", fake_race)
                    logging.info("self.race %s", self.race)
                    # Start playback task
                    self._playback_task = asyncio.create_task(self.play_fake_race(fake_race))
                else:
                    # Real race mode - prepare / start real hardware monitoring or race input
                    logging.info("Starting real race")
                    # Send reset command to hardware_comm_process using multiprocessing queue
                    self._hardware_in_queue.put({"type": "command", "command": "start_race"})

        asyncio.create_task(start_race_and_send_command())

    def action_end_race(self) -> None:
        status_display = self.query_one(RaceStatusDisplay)
        start_btn = self.query_one("#start_btn", Button)
        stop_btn = self.query_one("#stop_btn", Button)
        if self.race.state == RaceState.RUNNING:
            # Stop playback and reset race state
            if hasattr(self, "_playback_task") and self._playback_task is not None and not self._playback_task.done():
                self._playback_task.cancel()
            self.race.state = RaceState.FINISHED
            status_display.race_state = self.race.state
            start_btn.disabled = False
            stop_btn.disabled = True

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "start_btn":
            self.action_start_race()
        elif button_id == "stop_btn":
            self.action_end_race()

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

        start_time = self.race.start_time

        if start_time is None:
            start_time = asyncio.get_event_loop().time()

        sorted_laps = order_laps_by_occurrence(fake_race.laps)

        logging.info("Sorted laps:\n%s", pprint.pformat(sorted_laps))
        cumulative_elapsed = 0.0

        try:
            for (ts, lap) in sorted_laps:
                elapsed_time = asyncio.get_event_loop().time() - start_time
                wait_time = ts - elapsed_time
                if wait_time > 0:
                    await asyncio.sleep(wait_time)
                lap_event = make_fake_lap(lap.racer_id, lap.lap_number, lap.lap_time)
                logging.info("fake lap %s", lap_event)
                await self.lap_queue.put(lap_event)
                cumulative_elapsed += lap.lap_time
        except asyncio.CancelledError:
            # Playback was stopped
            pass



if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Start Franklin Lap Counter in chosen initial mode.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--race', action='store_true', help='Start in Race Mode (Real Race Mode)')
    group.add_argument('--fake', action='store_true', help='Start in Fake Race Mode')
    group.add_argument('--training', action='store_true', help='Start in Training Mode')
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

    app = HardwareMonitorGUI(initial_mode=initial_mode)
    app.run()
