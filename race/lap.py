from dataclasses import dataclass, field
from typing import NewType

# Wrap raw float times in types for clarity.
InternalLapTime = NewType('InternalLapTime', float)
SecondsFromRaceStart = NewType('SecondsFromRaceStart', float)
LapTime = NewType('LapTime', float)

@dataclass(order=True)
class Lap:
    """Represents a single lap completed by a racer.

    A lap now records two time values:
      • seconds_from_race_start: The time reported by the lap counter hardware.
      • internal_lap_time: The event loop’s monotonic time.
    """
    racer_id: int = field(compare=False)
    lap_number: int

    seconds_from_race_start: SecondsFromRaceStart
    internal_lap_time: InternalLapTime = field(compare=False)
    lap_time: LapTime = field(compare=False)

    def __post_init__(self):
        if self.lap_number < 0:
            raise ValueError("Lap number must be >= 0")
        if self.seconds_from_race_start <= 0:
            raise ValueError("Seconds from race start must be positive")
        if self.internal_lap_time <= 0:
            raise ValueError("Internal lap time must be positive")

    def __str__(self):
        return (f"Racer {self.racer_id} Lap {self.lap_number} | "
                f"Hardware: {self.seconds_from_race_start:.2f}s, "
                f"Internal: {self.internal_lap_time:.2f}s")

    def __repr__(self):
        return (f"Lap(racer_id={self.racer_id}, lap_number={self.lap_number}, "
                f"seconds_from_race_start={self.seconds_from_race_start}, "
                f"internal_lap_time={self.internal_lap_time})")

    def is_better_than(self, other: "Lap") -> bool:
        """
        Compare this lap's quality to another lap by lap_number, then by hardware time.
        A higher lap number is better; if equal, a lower hardware time is better.
        """
        if self.lap_number != other.lap_number:
            return self.lap_number > other.lap_number
        return self.seconds_from_race_start < other.seconds_from_race_start
