from dataclasses import dataclass, field
from typing import NewType

# Epoch-based timestamps (seconds since Unix epoch).
EpochSeconds = NewType("EpochSeconds", float)
LapTime = NewType("LapTime", float)


@dataclass(order=True)
class Lap:
    """Represents one lap event using absolute timestamps.

    Canonical time fields:
      - race_start_at: race start timestamp (epoch seconds)
      - lap_at: when this lap crossing occurred (epoch seconds)
      - recorded_at: when this event was recorded/ingested (epoch seconds)

    `lap_time` remains the lap interval in seconds for leaderboard/best-lap logic.
    """

    racer_id: int = field(compare=False)
    lap_number: int
    race_start_at: EpochSeconds
    lap_at: EpochSeconds
    recorded_at: EpochSeconds = field(compare=False)
    lap_time: LapTime = field(compare=False)

    def __post_init__(self) -> None:
        if self.lap_number < 0:
            raise ValueError("Lap number must be >= 0")
        if self.race_start_at <= 0:
            raise ValueError("race_start_at must be positive")
        if self.lap_at <= 0:
            raise ValueError("lap_at must be positive")
        if self.recorded_at <= 0:
            raise ValueError("recorded_at must be positive")
        if self.lap_at < self.race_start_at:
            raise ValueError("lap_at must be >= race_start_at")

    @property
    def seconds_from_race_start(self) -> float:
        """Compatibility accessor for older code paths."""
        return float(self.lap_at - self.race_start_at)

    @property
    def internal_lap_time(self) -> float:
        """Deprecated compatibility accessor used by older displays."""
        return float(self.lap_time)

    def __str__(self) -> str:
        return (
            f"Racer {self.racer_id} Lap {self.lap_number} | "
            f"LapAt={self.lap_at:.3f}, StartAt={self.race_start_at:.3f}, "
            f"RecordedAt={self.recorded_at:.3f}, "
            f"Elapsed={self.seconds_from_race_start:.2f}s, Lap Time={self.lap_time:.2f}s"
        )

    def __repr__(self) -> str:
        return (
            "Lap("
            f"racer_id={self.racer_id}, "
            f"lap_number={self.lap_number}, "
            f"race_start_at={self.race_start_at}, "
            f"lap_at={self.lap_at}, "
            f"recorded_at={self.recorded_at}, "
            f"lap_time={self.lap_time}"
            ")"
        )

    def is_better_than(self, other: "Lap") -> bool:
        """Compare lap quality by lap_number, then by earlier absolute lap timestamp."""
        if self.lap_number != other.lap_number:
            return self.lap_number > other.lap_number
        return self.lap_at < other.lap_at
