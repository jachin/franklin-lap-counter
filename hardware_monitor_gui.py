import asyncio
import logging
import random
from textual.app import App, ComposeResult
from textual.containers import Vertical, Horizontal
from textual.widgets import Header, Footer, Static, Button, TabbedContent, TabPane
from textual.reactive import reactive
from race.lap import Lap
from race.race import Race, RaceState

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

class LeaderboardDisplay(Static):
    leaderboard = reactive([])

    def render(self) -> str:
        if not self.leaderboard:
            return "No laps completed yet."
        lines = ["Leaderboard:"]
        for pos, (racer_id, lap_count, best_lap_time, total_time) in enumerate(self.leaderboard, 1):
            lines.append(
                f" {pos}. Racer {racer_id}: Lap {lap_count}, Best Lap {best_lap_time:.2f}s, Total Time {total_time:.2f}s"
            )
        return "\n".join(lines)

class RaceTimeDisplay(Static):
    elapsed_time = reactive(0.0)

    def render(self) -> str:
        seconds = int(self.elapsed_time)
        tenths = int((self.elapsed_time - seconds)*10)
        return f"Race Time: {seconds}.{tenths}s"

class HardwareMonitorGUI(App):
    CSS = """
    Screen {
        align: center middle;
    }
    #main_container {
        width: 80%;
        height: 80%;
        border: heavy green;
    }
    #race_controls {
        height: 10;
        content-align: center middle;
    }
    #race_time {
        height: 3;
        content-align: center middle;
        border: magenta;
        padding: 1 1;
        margin-top: 0;
        max-width: 20;
    }
    #race_status {
        height: 3;
        content-align: center middle;
        border: heavy blue;
        padding: 1 2;
        margin-bottom: 1;
        max-width: 20;
    }
    #tabbed_content {
        border: heavy cyan;
        height: 1fr;
        padding: 1 2;
    }

    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.lap_queue = asyncio.Queue()
        self.race = Race()
        self._racer_ids = [101, 102, 103, 104, 105]

        # Setup logging
        logging.basicConfig(
            filename='race.log',
            filemode='a',
            format='%(asctime)s %(levelname)s:%(message)s',
            level=logging.INFO
        )
        logging.info("HardwareMonitorGUI initialized")

    async def hardware_monitor_task(self):
        """Simulated hardware monitor with 5 racers doing laps around 5 seconds +/- 1s."""
        if not hasattr(self, "_lap_counters") or not self._lap_counters:
            # Initialize lap counters for each racer
            self._lap_counters = {racer_id: 0 for racer_id in self._racer_ids}
        if not hasattr(self, "_racer_lap_times") or not self._racer_lap_times:
            self._racer_lap_times = {}

        while True:
            if self.race.state == RaceState.RUNNING:
                if self.race.start_time is None:
                    start_time = asyncio.get_event_loop().time()
                    self.race.start(start_time=start_time)
                    # Fix lap times once at start for each racer
                    for racer in self._racer_ids:
                        self._racer_lap_times[racer] = 5.0 + random.uniform(-1, 1)
                    logging.info(f"Race started at {start_time}")
                    logging.info(f"Lap times per racer: {self._racer_lap_times}")

                now = asyncio.get_event_loop().time()
                if self.race.start_time is None:
                    elapsed = 0.0
                else:
                    elapsed = now - self.race.start_time
                self.race.elapsed_time = elapsed
                next_lap_events = []

                # Calculate which racers have completed new laps based on cumulative lap times
                for racer_id in self._racer_ids:
                    last_lap_number = self._lap_counters.get(racer_id, 0)
                    next_lap_number = last_lap_number + 1
                    threshold_time = self._racer_lap_times[racer_id] * next_lap_number
                    if elapsed >= threshold_time:
                        next_lap_events.append((racer_id, next_lap_number, self._racer_lap_times[racer_id]))

                # Emit lap events for all racers that completed laps
                for racer_id, lap_number, lap_time in next_lap_events:
                    logging.info(f"Emitting lap {lap_number} for racer {racer_id} at lap time {lap_time}")
                    self._lap_counters[racer_id] = lap_number
                    lap = Lap(
                        racer_id=racer_id, lap_number=lap_number, lap_time=lap_time
                    )
                    await self.lap_queue.put(lap)

                # Sleep a bit before checking again
                await asyncio.sleep(0.1)
            else:
                if self.race.start_time is not None:
                    logging.info("Race stopped/reset")
                self.race.start_time = None
                self.race.elapsed_time = 0.0
                self._lap_counters.clear()
                self._racer_lap_times.clear()
                await asyncio.sleep(0.1)

    async def refresh_lap_data(self):
        logging.info("refresh_lap_data")
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
            with Horizontal(id="race_controls"):
                with Vertical():
                    yield Button("Start Race", id="start_btn")
                    yield Button("Stop Race", id="stop_btn", disabled=True)
                yield RaceTimeDisplay(name="Race Time", id="race_time", classes="box")
                yield RaceStatusDisplay(id="race_status", classes="box")
            with TabbedContent(id="tabbed_content"):
                with TabPane("Leaderboard", id="leaderboard_tab"):
                    yield LeaderboardDisplay(id="leaderboard")
                with TabPane("Events", id="events_tab"):
                    yield LapDataDisplay(id="lap_data")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        status_display = self.query_one(RaceStatusDisplay)
        start_btn = self.query_one("#start_btn", Button)
        stop_btn = self.query_one("#stop_btn", Button)

        if button_id == "start_btn":
            current_time = asyncio.get_event_loop().time()
            self.race.start(start_time=current_time)
            status_display.race_state = self.race.state
            start_btn.disabled = True
            stop_btn.disabled = False
        elif button_id == "stop_btn":
            self.race.pause()
            status_display.race_state = self.race.state
            start_btn.disabled = False
            stop_btn.disabled = True

    async def on_mount(self) -> None:
        asyncio.create_task(self.refresh_lap_data())
        asyncio.create_task(self.hardware_monitor_task())


if __name__ == "__main__":
    app = HardwareMonitorGUI()
    app.run()
