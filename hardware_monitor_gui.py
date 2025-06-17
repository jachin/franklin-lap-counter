import asyncio
import logging
from textual.app import App, ComposeResult
from textual.containers import Vertical, Horizontal
from textual.widgets import Header, Footer, Static, Button, TabbedContent, TabPane, Digits, DataTable
from textual.reactive import reactive
from race.lap import Lap
from race.race import Race, RaceState
from race.race import generate_fake_race, order_laps_by_occurrence
from textual.binding import Binding
import pprint
import serial_asyncio
from serial.serialutil import SerialException

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

    def render(self) -> str:
        if self.race_state == RaceState.RUNNING:
            return "Race started"
        elif self.race_state == RaceState.PAUSED:
            return "Race paused"
        elif self.race_state == RaceState.FINISHED:
            return "Race finished"
        else:
            return "Race not started"

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

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.lap_queue = asyncio.Queue()
        self.race = Race()
        self.fake_race_mode = True  # Default to fake race mode
        self.lap_counter_detected = reactive(False)
        self._last_lap_counter_signal_time = None

        # Setup logging
        logging.basicConfig(
            filename='race.log',
            filemode='a',
            format='%(asctime)s %(levelname)s:%(message)s',
            level=logging.INFO
        )
        logging.info("HardwareMonitorGUI initialized")

    def update_subtitle(self) -> None:
        mode_str = "Fake Race Mode" if self.fake_race_mode else "Real Race Mode"
        self.sub_title = f"RC Lap Counter - {mode_str}"
        try:
            header = self.query_one(Header)
            header.refresh()
        except Exception:
            # Header widget not found yet
            pass

    def action_toggle_mode(self) -> None:
        self.fake_race_mode = not self.fake_race_mode
        mode_str = "Fake Race Mode" if self.fake_race_mode else "Real Race Mode"
        logging.info(f"Toggled race mode to: {mode_str}")
        self.update_subtitle()

    async def update_race_time(self):
        # TODO this works for now but we should probably use the time that's coming
        # from the lap counter it self
        while True:
            if self.race.state == RaceState.RUNNING and self.race.start_time is not None:
                self.race.elapsed_time = asyncio.get_event_loop().time() - self.race.start_time
            await asyncio.sleep(0.1)

    async def hardware_monitor_task(self):
        while True:
            try:
                logging.info("Connecting to hardware monitor")
                self.lap_counter_reader, self.lap_counter_writer = await serial_asyncio.open_serial_connection(
                    url='/dev/ttyUSB0', baudrate=9600)

                # Reset detection on fresh connection attempt
                self.lap_counter_detected = False
                self._last_lap_counter_signal_time = None

                while True:
                    try:
                        line_bytes = await asyncio.wait_for(self.lap_counter_reader.readline(), timeout=0.1)
                        if line_bytes:
                            line = line_bytes.decode('utf-8').strip()
                            logging.info("Received: %s", line)

                            # Check if the line matches the lap counter detected signal pattern
                            # Example expected pattern: "#        202     14272   0       xC249"
                            if line.startswith("#") and "xC249" in line:
                                if not self.lap_counter_detected:
                                    logging.info("Lap counter detected (signal received)")
                                self.lap_counter_detected = True
                                self._last_lap_counter_signal_time = asyncio.get_event_loop().time()

                            # Check if more than 2 seconds have elapsed without signal
                            if self._last_lap_counter_signal_time is not None:
                                elapsed = asyncio.get_event_loop().time() - self._last_lap_counter_signal_time
                                if elapsed > 2 and self.lap_counter_detected:
                                    logging.info("Lap counter lost (no signal for > 2 seconds)")
                                    self.lap_counter_detected = False

                        else:
                            # EOF or no data
                            await asyncio.sleep(0.1)
                    except asyncio.TimeoutError:
                        # No data received within timeout
                        # Also check if more than 2 seconds have elapsed without signal
                        if self._last_lap_counter_signal_time is not None:
                            elapsed = asyncio.get_event_loop().time() - self._last_lap_counter_signal_time
                            if elapsed > 2 and self.lap_counter_detected:
                                logging.info("Lap counter lost (no signal for > 2 seconds)")
                                self.lap_counter_detected = False
            except SerialException as e:
                logging.error(f"SerialException in hardware_monitor_task: {e}")
                if self.lap_counter_detected:
                    logging.info("Lap counter lost (SerialException detected)")
                self.lap_counter_detected = False

                # Close writer if open
                if hasattr(self, 'lap_counter_writer') and self.lap_counter_writer is not None:
                    try:
                        self.lap_counter_writer.close()
                        await self.lap_counter_writer.wait_closed()
                    except Exception as close_exc:
                        logging.warning(f"Exception while closing serial connection after error: {close_exc}")

                # Wait before retrying connection
                await asyncio.sleep(2)

    async def refresh_lap_data(self):
        lap_display_events = self.query_one(LapDataDisplay)
        lap_display_leaderboard = self.query_one(LeaderboardDisplay)
        race_time_display = self.query_one(RaceTimeDisplay)
        while True:
            logging.info("refresh_lap_data loop %s", self.race.elapsed_time)
            race_time_display.elapsed_time = self.race.elapsed_time
            try:
                lap = await asyncio.wait_for(self.lap_queue.get(), timeout=0.1)
                self.race.add_lap(lap)
                lap_display_events.laps = self.race.laps.copy()
                lap_display_leaderboard.leaderboard = self.race.leaderboard()
            except asyncio.TimeoutError:
                # No new lap data, just refresh displays
                lap_display_events.laps = self.race.laps.copy()
                lap_display_leaderboard.leaderboard = self.race.leaderboard()


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

    async def send_command_to_lap_counter(self):
        if (hasattr(self, 'lap_counter_writer') and self.lap_counter_writer is not None
                and not self.fake_race_mode and self.lap_counter_detected):
            try:
                # Send the byte series commands from the prototype
                commands = [
                    b'\x01\x3f\x2c\x32\x33\x32\x2c\x30\x2c\x31\x34\x2c\x30\x2c\x31\x2c\x0d\x0a',
                    b'\x01\x3f\x2c\x32\x33\x32\x2c\x30\x2c\x32\x34\x2c\x30\x2c\x0d\x0a',
                    b'\x01\x3f\x2c\x32\x33\x32\x2c\x30\x2c\x39\x2c\x30\x2c\x0d\x0a',
                    b'\x01\x3f\x2c\x32\x33\x32\x2c\x30\x2c\x31\x34\x2c\x31\x2c\x30\x0d\x0a',
                ]
                for command in commands:
                    self.lap_counter_writer.write(command)
                    await self.lap_counter_writer.drain()
                logging.info("Sent start race commands to lap counter")
            except Exception as e:
                logging.error(f"Error sending command to lap counter: {e}")

    def action_start_race(self) -> None:
        async def start_race_and_send_command():
            status_display = self.query_one(RaceStatusDisplay)
            start_btn = self.query_one("#start_btn", Button)
            stop_btn = self.query_one("#stop_btn", Button)

            if self.race.state != RaceState.RUNNING:
                current_time = asyncio.get_event_loop().time()
                self.race.start(start_time=current_time)

                status_display.race_state = self.race.state
                start_btn.disabled = True
                stop_btn.disabled = False

                if hasattr(self, "_playback_task") and not self._playback_task.done():
                    self._playback_task.cancel()

                if self.fake_race_mode:
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
                    # Send command to lap counter after starting hardware monitor
                    await self.send_command_to_lap_counter()

        asyncio.create_task(start_race_and_send_command())

    def action_end_race(self) -> None:
        status_display = self.query_one(RaceStatusDisplay)
        start_btn = self.query_one("#start_btn", Button)
        stop_btn = self.query_one("#stop_btn", Button)
        if self.race.state == RaceState.RUNNING:
            # Stop playback and reset race state
            if hasattr(self, "_playback_task") and not self._playback_task.done():
                self._playback_task.cancel()
            self.race.reset()
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
                lap_event = Lap(
                    racer_id=lap.racer_id,
                    lap_number=lap.lap_number,
                    lap_time=lap.lap_time,
                )
                logging.info("fake lap %s", lap_event)
                await self.lap_queue.put(lap_event)
                cumulative_elapsed += lap.lap_time
        except asyncio.CancelledError:
            # Playback was stopped
            pass



if __name__ == "__main__":
    app = HardwareMonitorGUI()
    app.run()
