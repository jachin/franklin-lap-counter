import unittest

from race.race_end_logic import resolve_post_lap_state
from race.race_state import RaceEndMode, RaceState


class TestResolvePostLapState(unittest.TestCase):
    def test_winner_mode_finishes_immediately(self):
        leaderboard = [(1, 1, 10, 9.1, 9.3, 90.0)]
        state = resolve_post_lap_state(
            current_state=RaceState.RUNNING,
            race_end_mode=RaceEndMode.WINNER,
            total_laps=10,
            leaderboard=leaderboard,
            active_contestants={1, 2},
        )
        self.assertEqual(state, RaceState.FINISHED)

    def test_last_car_mode_declares_winner_until_all_finish(self):
        leaderboard = [
            (1, 1, 10, 9.0, 9.1, 90.0),
            (2, 2, 9, 9.2, 9.5, 95.0),
        ]
        state = resolve_post_lap_state(
            current_state=RaceState.RUNNING,
            race_end_mode=RaceEndMode.LAST_CAR,
            total_laps=10,
            leaderboard=leaderboard,
            active_contestants={1, 2},
        )
        self.assertEqual(state, RaceState.WINNER_DECLARED)

    def test_last_car_mode_finishes_when_all_active_finish(self):
        leaderboard = [
            (1, 1, 10, 9.0, 9.1, 90.0),
            (2, 2, 10, 9.2, 9.4, 98.0),
        ]
        state = resolve_post_lap_state(
            current_state=RaceState.WINNER_DECLARED,
            race_end_mode=RaceEndMode.LAST_CAR,
            total_laps=10,
            leaderboard=leaderboard,
            active_contestants={1, 2},
        )
        self.assertEqual(state, RaceState.FINISHED)

    def test_manual_mode_never_auto_finishes(self):
        leaderboard = [(1, 1, 10, 9.0, 9.2, 90.0)]
        state = resolve_post_lap_state(
            current_state=RaceState.RUNNING,
            race_end_mode=RaceEndMode.MANUAL,
            total_laps=10,
            leaderboard=leaderboard,
            active_contestants={1},
        )
        self.assertEqual(state, RaceState.RUNNING)


if __name__ == "__main__":
    unittest.main()
