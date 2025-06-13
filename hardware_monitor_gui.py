import asyncio
import logging
from textual.app import App, ComposeResult
from textual.containers import Vertical, Horizontal
from textual.widgets import Header, Footer, Static, Button, TabbedContent, TabPane
from textual.reactive import reactive
from race.lap import Lap
from race.race import Race, RaceState
from race.race import generate_fake_race, order_laps_by_occurrence
import pprint

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
        #TODO Implement hardware monitor task
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

            # Generate a fake race
            fake_race = generate_fake_race()

            logging.info("fake_race %s", fake_race)
            logging.info("self.race %s", self.race)

            status_display.race_state = self.race.state
            start_btn.disabled = True
            stop_btn.disabled = False

            # Start playback task
            if hasattr(self, "_playback_task") and not self._playback_task.done():
                self._playback_task.cancel()
            self._playback_task = asyncio.create_task(self.play_fake_race(fake_race))
        elif button_id == "stop_btn":
            # Stop playback and reset race state
            if hasattr(self, "_playback_task") and not self._playback_task.done():
                self._playback_task.cancel()
            self.race.reset()
            status_display.race_state = self.race.state
            start_btn.disabled = False
            stop_btn.disabled = True

    async def on_mount(self) -> None:
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
