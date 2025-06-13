from enum import Enum, auto
from typing import List, Optional
from .lap import Lap


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

    def start(self) -> None:
        if self.state in (RaceState.NOT_STARTED, RaceState.PAUSED):
            self.state = RaceState.RUNNING

    def pause(self) -> None:
        if self.state == RaceState.RUNNING:
            self.state = RaceState.PAUSED

    def finish(self) -> None:
        self.state = RaceState.FINISHED

    def reset(self) -> None:
        self.laps.clear()
        self.state = RaceState.NOT_STARTED

    def add_lap(self, lap: Lap) -> None:
        if self.state != RaceState.RUNNING:
            raise RuntimeError("Cannot add lap unless race is running")
        self.laps.append(lap)

    def leaderboard(self) -> List:
        """
        Returns a leaderboard as a list of tuples:
        (racer_id, lap_count, best_lap_time, total_time)
        sorted by lap_count descending, then best_lap_time ascending.
        """
        stats = {}
        for lap in self.laps:
            rid = lap.racer_id
            if rid not in stats:
                stats[rid] = {
                    "lap_count": 1,
                    "best_lap_time": lap.lap_time,
                    "total_time": lap.lap_time,
                }
            else:
                stats[rid]["lap_count"] += 1
                stats[rid]["total_time"] += lap.lap_time
                if lap.lap_time < stats[rid]["best_lap_time"]:
                    stats[rid]["best_lap_time"] = lap.lap_time
        sorted_stats = sorted(
            stats.items(),
            key=lambda item: (-item[1]["lap_count"], item[1]["best_lap_time"]),
        )
        return [
            (racer_id, data["lap_count"], data["best_lap_time"], data["total_time"])
            for racer_id, data in sorted_stats
        ]

    def best_lap(self) -> Optional[Lap]:
        """Returns the best (fastest) lap out of all laps in the race, or None if no laps."""
        if not self.laps:
            return None
        return min(self.laps, key=lambda lap: lap.lap_time)
