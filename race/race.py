from enum import Enum, auto
from typing import List, Optional, Tuple, Set
import random
import logging
from race.lap import Lap, SecondsFromRaceStart, InternalLapTime, LapTime


def generate_fake_race():
    """Generates a fake race with 5 drivers, 10 laps each, and random lap times between 5 and 6 seconds."""
    fake_race = Race()
    logging.info("Generating new fake race")
    fake_race.start(start_time=0.0)
    logging.info("Started fake race")

    # Start with lap 0 for each racer to simulate them crossing the start line
    racer_ids = [1, 2, 3, 4, 5]

    # Track cumulative times for each racer
    racer_cumulative_times = {racer_id: 0.0 for racer_id in racer_ids}

    # Add start triggers (lap 0)
    for racer_id in racer_ids:
        # Each racer takes 1-2 seconds to reach the start line from their position
        start_time = random.uniform(1.0, 2.0)
        racer_cumulative_times[racer_id] = start_time
        logging.debug(f"Adding start trigger for racer {racer_id} at {start_time:.2f}s")
        fake_race.add_fake_lap(
            Lap(
                racer_id=racer_id,
                lap_number=0,  # Lap 0 = initial start line crossing
                seconds_from_race_start=SecondsFromRaceStart(start_time),
                internal_lap_time=InternalLapTime(start_time),
                lap_time=LapTime(start_time)
            )
        )

    # Now generate actual race laps with proper per-racer timing
    for lap_number in range(1, 11):
        logging.debug(f"Generating lap {lap_number} for all racers")
        for racer_id in racer_ids:
            lap_time = random.uniform(5, 6)
            racer_cumulative_times[racer_id] += lap_time
            cumulative_time = racer_cumulative_times[racer_id]

            logging.debug(f"Adding lap {lap_number} for racer {racer_id} at {cumulative_time:.2f}s with time {lap_time:.2f}s")
            # For a fake lap, use the same value for hardware and internal times.
            fake_race.add_fake_lap(
                Lap(
                    racer_id=racer_id,
                    lap_number=lap_number,
                    seconds_from_race_start=SecondsFromRaceStart(cumulative_time),
                    internal_lap_time=InternalLapTime(lap_time),
                    lap_time=LapTime(lap_time)
                )
            )
    logging.info(f"Generated fake race with {len(fake_race.laps)} laps")
    return fake_race


class RaceState(Enum):
    NOT_STARTED = auto()
    RUNNING = auto()
    PAUSED = auto()
    WINNER_DECLARED = auto()
    FINISHED = auto()



class Race:
    """Manages a race with laps and state, computes leaderboards and best lap."""

    def __init__(self, *, previous_race: Optional['Race'] = None):
        self.laps: List[Lap] = []
        self.state: RaceState = RaceState.NOT_STARTED
        self.start_time: Optional[float] = None
        self.elapsed_time: float = 0.0
        self.total_laps: int = 10  # Race ends after 10 laps
        self.active_contestants: Set[int] = set()  # Set of transmitter_ids

        # If we have a previous race, copy its settings and active contestants
        if previous_race:
            self.total_laps = previous_race.total_laps
            self.active_contestants = {lap.racer_id for lap in previous_race.laps if lap.lap_number > 0}

    def start(self, start_time: float) -> None:
        if self.state in (RaceState.NOT_STARTED, RaceState.PAUSED):
            self.state = RaceState.RUNNING
            self.start_time = start_time
            self.elapsed_time = 0.0
            logging.info(f"Race started at {start_time:.2f}")

    def pause(self) -> None:
        if self.state == RaceState.RUNNING:
            self.state = RaceState.PAUSED

    def finish(self) -> None:
        self.state = RaceState.FINISHED
        logging.info(f"Race finished with {len(self.active_contestants)} active contestants")

    def reset(self) -> None:
        self.laps.clear()
        self.state = RaceState.NOT_STARTED
        self.start_time = None
        self.elapsed_time = 0.0
        self.active_contestants.clear()


    def get_active_contestant_ids(self) -> Set[int]:
        """Returns the set of transmitter IDs for contestants who are active in this race."""
        return self.active_contestants

    def has_contestant(self, transmitter_id: int) -> bool:
        """Returns True if the given transmitter_id is an active contestant in this race."""
        return transmitter_id in self.active_contestants

    def add_lap(self, lap: Lap) -> None:
        if self.state not in (RaceState.RUNNING, RaceState.WINNER_DECLARED):
            raise RuntimeError("Cannot add lap unless race is running or waiting for other racers to finish")

        logging.info(f"adding lap: {self.state}")

        # Add contestant to active racers if this is their first lap
        if lap.lap_number > 0:  # Don't add for lap 0 which is just start trigger
            if lap.racer_id not in self.active_contestants:
                logging.info(f"Adding new active contestant {lap.racer_id}")
            self.active_contestants.add(lap.racer_id)
            logging.debug(f"Added racer {lap.racer_id} to active contestants. Active: {self.active_contestants}")

        self.laps.append(lap)
        logging.info(f"Lap added - Racer: {lap.racer_id}, Lap: {lap.lap_number}, Time: {lap.lap_time:.2f}")

        leaderboard = self.leaderboard()
        if leaderboard:
            leader_position, leader_id, leader_laps, _, _, _ = leaderboard[0]
            logging.debug(f"Current leader: Racer {leader_id} with {leader_laps} laps")

            # First racer to complete all laps is the winner
            if leader_laps >= self.total_laps and self.state == RaceState.RUNNING:
                self.state = RaceState.WINNER_DECLARED
                logging.info(f"Winner declared! Racer {leader_id} finished {leader_laps} laps")

            # Only check for race completion if we have active contestants
            if self.active_contestants:
                # Check if all active racers have finished their laps
                all_active_finished = True
                for position, racer_id, lap_count, best_lap, last_lap, total_time in leaderboard:
                    if racer_id in self.active_contestants:
                        logging.debug(f"Checking racer {racer_id}: {lap_count}/{self.total_laps} laps")
                        if lap_count < self.total_laps:
                            all_active_finished = False
                            break

                # If all active racers are done, finish the race
                if all_active_finished:
                    logging.info(f"All active racers ({self.active_contestants}) have completed their laps - Race finished!")
                    self.finish()

    def add_fake_lap(self, lap: Lap) -> None:
        """Add a fake lap during race simulation. Also adds the racer to active contestants."""
        self.laps.append(lap)
        if lap.lap_number > 0:  # Don't add for lap 0 which is just start trigger
            self.active_contestants.add(lap.racer_id)

    def leaderboard(self) -> List:
        """
        Returns a leaderboard as a list of tuples:
        (position, racer_id, lap_count, best_lap_time, last_lap_time, total_time)
        sorted by lap_count descending, then best_lap_time ascending,
        with explicit position assigned.
        """
        stats = {}
        for lap in self.laps:
            rid = lap.racer_id
            if rid not in stats:
                # Exclude lap 0 from lap count and best lap time
                stats[rid] = {
                    "lap_count": 0 if lap.lap_number == 0 else 1,
                    "best_lap_time": float('inf') if lap.lap_number == 0 else lap.lap_time,
                    "last_lap_time": lap.lap_time,
                    "total_time": lap.seconds_from_race_start,
                }
            else:
                if lap.lap_number > 0:
                    stats[rid]["lap_count"] += 1
                    if lap.lap_time < stats[rid]["best_lap_time"]:
                        stats[rid]["best_lap_time"] = lap.lap_time
                    stats[rid]["last_lap_time"] = lap.lap_time
                # Always update total_time to the latest lap's seconds_from_race_start
                if lap.seconds_from_race_start > stats[rid]["total_time"]:
                    stats[rid]["total_time"] = lap.seconds_from_race_start
        sorted_stats = sorted(
            stats.items(),
            key=lambda item: (-item[1]["lap_count"], item[1]["best_lap_time"]),
        )
        leaderboard_with_position = []
        position = 1
        for racer_id, data in sorted_stats:
            leaderboard_with_position.append(
                (position, racer_id, data["lap_count"], data["best_lap_time"], data["last_lap_time"], data["total_time"])
            )
            position += 1
        return leaderboard_with_position

    def laps_remaining(self) -> Tuple[int, int]:
        """Returns tuple of (leader_laps_remaining, last_place_laps_remaining)"""
        leaderboard = self.leaderboard()
        if not leaderboard:
            return (self.total_laps, self.total_laps)

        leader_laps = leaderboard[0][2]
        last_place_laps = leaderboard[-1][2]

        leader_remaining = max(0, self.total_laps - leader_laps)
        last_remaining = max(0, self.total_laps - last_place_laps)

        return (leader_remaining, last_remaining)

    def best_lap(self) -> Optional[Lap]:
        """Returns the best (fastest) lap out of all laps in the race, or None if no laps."""
        if not self.laps:
            return None
        return min(self.laps, key=lambda lap: lap.lap_time)

    def __str__(self) -> str:
        leader_remaining, last_remaining = self.laps_remaining()
        return (
            f"Race(state={self.state.name}, "
            f"laps={len(self.laps)}, "
            f"start_time={self.start_time}, "
            f"elapsed_time={self.elapsed_time:.2f}, "
            f"leader_remaining={leader_remaining}, "
            f"last_remaining={last_remaining}, "
            f"active_contestants={len(self.active_contestants)})"
        )

    def __repr__(self) -> str:
        return (
            f"Race(state={self.state}, "
            f"start_time={self.start_time!r}, "
            f"elapsed_time={self.elapsed_time!r}, "
            f"laps=[{', '.join(repr(lap) for lap in self.laps)}], "
            f"active_contestants={self.active_contestants!r})"
        )

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


def make_lap_from_sensor_data_and_race(racer_id: int, race_time: float, interal_time: float, race: Race) -> Lap:
    lap_number = sum(1 for lap in race.laps if lap.racer_id == racer_id) or 0
    if race.start_time is None:
        raise ValueError("Race has not started")

    # Get the race_time for the last lap for this racer_id
    # if we don't have one then this must be the first lap to it should be zero.
    laps = list(filter(lambda lap: lap.racer_id == racer_id, race.laps))

    if len(laps) == 0:
        lap_time = LapTime(race_time)
    else:
        lap_time = race_time - laps[-1].seconds_from_race_start

    new_lap = Lap(
        racer_id=racer_id,
        lap_number=lap_number,
        seconds_from_race_start=SecondsFromRaceStart(race_time),
        internal_lap_time=InternalLapTime(interal_time),
        lap_time=LapTime(lap_time)
    )

    return new_lap

def make_fake_lap(racer_id: int, lap_number: int, lap_time: float, seconds_from_start: float) -> Lap:
    """
    Create a fake lap with proper timing values.

    Parameters:
    - racer_id: The ID of the racer
    - lap_number: The lap number (0 for start trigger)
    - lap_time: The time for this individual lap
    - seconds_from_start: Cumulative time since race start
    """
    return Lap(
        racer_id=racer_id,
        lap_number=lap_number,
        seconds_from_race_start=SecondsFromRaceStart(seconds_from_start),
        internal_lap_time=InternalLapTime(lap_time),
        lap_time=LapTime(lap_time)
    )

# If a race is RUNNING or a WINNER_DECLARED we still consider the race going.False
def is_race_going(race: Race) -> bool:
    return race.state == RaceState.RUNNING or race.state == RaceState.WINNER_DECLARED
