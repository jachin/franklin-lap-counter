from enum import Enum, auto


class RaceState(Enum):
    NOT_STARTED = auto()
    RUNNING = auto()
    PAUSED = auto()
    WINNER_DECLARED = auto()
    FINISHED = auto()


class RaceEndMode(Enum):
    WINNER = "winner"
    LAST_CAR = "last_car"
    MANUAL = "manual"


def is_race_going_state(state: RaceState) -> bool:
    return state in (RaceState.RUNNING, RaceState.WINNER_DECLARED)
