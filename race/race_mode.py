from enum import Enum

class RaceMode(Enum):
    FAKE = "Fake Race Mode"
    REAL = "Real Race Mode"
    TRAINING = "Training Mode"

    def __str__(self):
        return self.value
