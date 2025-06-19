from enum import Enum, auto
from typing import List, Optional, Tuple
import random
from .lap import Lap, SecondsFromRaceStart, InternalLapTime, LapTime

def generate_fake_race():
    """Generates a fake race with 5 drivers, 10 laps each, and random lap times between 5 and 6 seconds."""
    fake_race = Race()
    racer_ids = [1, 2, 3, 4, 5]
    for lap_number in range(1, 11):
        seconds_from_race_start = 0
        for racer_id in racer_ids:
            lap_time = random.uniform(5, 6)

            seconds_from_race_start += lap_time

            # For a fake lap, use the same value for hardware and internal times.
            fake_race.add_fake_lap(
                Lap(
                    racer_id=racer_id,
                    lap_number=lap_number,
                    seconds_from_race_start=SecondsFromRaceStart(seconds_from_race_start),
                    internal_lap_time=InternalLapTime(lap_time),
                    lap_time=LapTime(lap_time)
                )
            )
    return fake_race


class RaceState(Enum):
    NOT_STARTED = auto()
    RUNNING = auto()
    PAUSED = auto()
    FINISHED = auto()


class Race:
    """Manages a race with laps and state, computes leaderboards and best lap."""

    def __init__(self):
        self.laps: List[Lap] = []
        self.state: RaceState = RaceState.NOT_STARTED
        self.start_time: Optional[float] = None
        self.elapsed_time: float = 0.0

    def start(self, start_time: float) -> None:
        if self.state in (RaceState.NOT_STARTED, RaceState.PAUSED):
            self.state = RaceState.RUNNING
            self.start_time = start_time

    def pause(self) -> None:
        if self.state == RaceState.RUNNING:
            self.state = RaceState.PAUSED

    def finish(self) -> None:
        self.state = RaceState.FINISHED

    def reset(self) -> None:
        self.laps.clear()
        self.state = RaceState.NOT_STARTED
        self.start_time = None
        self.elapsed_time = 0.0

    def add_lap(self, lap: Lap) -> None:
        if self.state != RaceState.RUNNING:
            raise RuntimeError("Cannot add lap unless race is running")
        self.laps.append(lap)

    def add_fake_lap(self, lap: Lap) -> None:
        self.laps.append(lap)

    def leaderboard(self) -> List:
        """
        Returns a leaderboard as a list of tuples:
        (position, racer_id, lap_count, best_lap_time, total_time)
        sorted by lap_count descending, then best_lap_time ascending,
        with explicit position assigned.
        """
        stats = {}
        for lap in self.laps:
            rid = lap.racer_id
            if rid not in stats:
                stats[rid] = {
                    "lap_count": 1,
                    "best_lap_time": lap.seconds_from_race_start,
                    "total_time": lap.seconds_from_race_start,
                }
            else:
                stats[rid]["lap_count"] += 1
                stats[rid]["total_time"] = lap.seconds_from_race_start
                if lap.seconds_from_race_start < stats[rid]["best_lap_time"]:
                    stats[rid]["best_lap_time"] = lap.seconds_from_race_start
        sorted_stats = sorted(
            stats.items(),
            key=lambda item: (-item[1]["lap_count"], item[1]["best_lap_time"]),
        )
        leaderboard_with_position = []
        position = 1
        for racer_id, data in sorted_stats:
            leaderboard_with_position.append(
                (position, racer_id, data["lap_count"], data["best_lap_time"], data["total_time"])
            )
            position += 1
        return leaderboard_with_position

    def best_lap(self) -> Optional[Lap]:
        """Returns the best (fastest) lap out of all laps in the race, or None if no laps."""
        if not self.laps:
            return None
        return min(self.laps, key=lambda lap: lap.lap_time)

def order_laps_by_occurrence(laps: List[Lap]) -> List[Tuple[float, Lap]]:
    """
    Given a list of Lap objects, returns a list of tuples ordered by the time
    the lap would have occurred in the race. Each tuple contains:
    (relative_time, lap) where relative_time is cumulative sum of lap times for the racer.
    Sorted by relative_time ascending.
    """

    laps_with_cumulative_time = []

    # Group laps by racer
    laps_by_racer = {}
    for lap in laps:
        laps_by_racer.setdefault(lap.racer_id, []).append(lap)

    # Sort laps per racer by lap number
    for racer_id, racer_laps in laps_by_racer.items():
        racer_laps.sort(key=lambda lap: lap.lap_number)

    # Calculate cumulative lap times per lap per racer
    for racer_id, racer_laps in laps_by_racer.items():
        total_time = 0.0
        for lap in racer_laps:
            total_time += lap.lap_time
            laps_with_cumulative_time.append((total_time, lap))

    # Sort all laps by their cumulative race time
    laps_with_cumulative_time.sort(key=lambda x: x[0])

    return laps_with_cumulative_time

    def __str__(self) -> str:
        return (
            f"Race(state={self.state.name}, "
            f"laps={len(self.laps)}, "
            f"start_time={self.start_time}, "
            f"elapsed_time={self.elapsed_time:.2f})"
        )

    def __repr__(self) -> str:
        return (
            f"Race(state={self.state}, "
            f"start_time={self.start_time!r}, "
            f"elapsed_time={self.elapsed_time!r}, "
            f"laps=[{', '.join(repr(lap) for lap in self.laps)}])"
        )

def make_lap_from_sensor_data_and_race(sensor_data: Tuple[int, float], interal_time: float, race: Race) -> Lap:
    racer_id, race_time = sensor_data
    lap_number = sum(1 for lap in race.laps if lap.racer_id == racer_id) or 0
    if race.start_time is None:
        raise ValueError("Race has not started")


    # Get the race_time for the last lap for this racer_id
    # if we don't have one then this must be the first lap to it should be zero.
    laps = list(filter(lambda lap: lap.racer_id == racer_id, race.laps))

    if len(laps) == 0:
        lap_time = LapTime(race_time)
    else:
        lap_time = race_time -laps[-1].seconds_from_race_start

    # For simplicity in this helper, we set internal time equal to hardware time.
    return Lap(
        racer_id=racer_id,
        lap_number=lap_number,
        seconds_from_race_start=SecondsFromRaceStart(race_time),
        internal_lap_time=InternalLapTime(interal_time),
        lap_time=LapTime(lap_time)
    )

def make_fake_lap(racer_id: int, lap_number: int, lap_time: float) -> Lap:
    return Lap(
        racer_id=racer_id,
        lap_number=lap_number,
        seconds_from_race_start=SecondsFromRaceStart(lap_time),
        internal_lap_time=InternalLapTime(lap_time),
        lap_time=LapTime(lap_time)
    )
