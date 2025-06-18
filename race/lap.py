from dataclasses import dataclass, field


@dataclass(order=True)
class Lap:
    """Represents a single lap completed by a racer."""
    racer_id: int = field(compare=False)
    lap_number: int
    lap_time: float

    def __post_init__(self):
        if self.lap_number < 0:
            raise ValueError("Lap number must be >= 0")
        if self.lap_time <= 0:
            raise ValueError("Lap time must be positive")

    def __str__(self):
        return f"Racer {self.racer_id} Lap {self.lap_number} Time: {self.lap_time:.2f}s"
    def __repr__(self):
        return f"Lap(racer_id={self.racer_id}, lap_number={self.lap_number}, lap_time={self.lap_time})"
    def is_better_than(self, other: "Lap") -> bool:
        """
        Compare this lap's quality to another lap by lap_number, then lap_time.
        Higher lap number is better; if equal, lower lap time is better.
        """
        if self.lap_number != other.lap_number:
            return self.lap_number > other.lap_number
        return self.lap_time < other.lap_time
