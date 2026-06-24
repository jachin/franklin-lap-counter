from typing import Optional
from .contestant import Contestant


class RaceContestants:
    """
    Manages race contestants and their display logic.
    """

    def __init__(self, contestants: Optional[list] = None):
        """
        Initialize RaceContestants with a list of dicts or Contestant objects.
        If dicts are provided, convert each to a Contestant object.
        """
        self.contestants: list[Contestant] = []
        if contestants:
            for c in contestants:
                if isinstance(c, Contestant):
                    self.contestants.append(c)
                elif isinstance(c, dict):
                    tid = c.get("transmitter_id")
                    name = c.get("name")
                    if tid is not None and name is not None:
                        self.contestants.append(Contestant(transmitter_id=tid, name=name))

    def get_contestant_name(self, transmitter_id: int) -> str:
        """
        Returns the name of the contestant with the given transmitter_id.
        If the transmitter_id is not found, returns a default string indicating unknown contestant.
        """
        for contestant in self.contestants:
            if contestant.transmitter_id == transmitter_id:
                return contestant.name
        return f"Unknown (ID: {transmitter_id})"

    def has_contestant(self, transmitter_id: int) -> bool:
        """Return whether a contestant exists for the transmitter ID."""
        return any(c.transmitter_id == transmitter_id for c in self.contestants)

    def ensure_contestant(self, transmitter_id: int) -> bool:
        """Add a placeholder contestant for an unknown transmitter ID.

        Returns True when a new contestant was added.
        """
        transmitter_id = int(transmitter_id)
        if transmitter_id <= 0 or self.has_contestant(transmitter_id):
            return False
        self.contestants.append(
            Contestant(
                transmitter_id=transmitter_id,
                name=f"Unknown (ID: {transmitter_id})",
            )
        )
        return True
