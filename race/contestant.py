from dataclasses import dataclass


@dataclass
class Contestant:
    """Represents a racer profile with transmitter ID, name, and metadata."""

    transmitter_id: int
    name: str
    # Future metadata fields can be added here, e.g.:
    # team: str = ""
    # vehicle_model: str = ""
    # color: str = ""

    def __str__(self) -> str:
        return f"{self.name} (Transmitter ID: {self.transmitter_id})"
