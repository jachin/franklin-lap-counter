import unittest

from race.race import Race, make_fake_lap
from race.race_state import RaceEndMode, RaceState


class TestRaceEndModesIntegration(unittest.TestCase):
    def test_last_car_mode_accepts_laps_after_winner_declared(self):
        race = Race(previous_race=None)
        race.total_laps = 2
        race.race_end_mode = RaceEndMode.LAST_CAR
        race.start(start_time=0.0)

        race.add_lap(make_fake_lap(1, 1, 10.0, 10.0))
        race.add_lap(make_fake_lap(2, 1, 10.5, 10.5))
        race.add_lap(make_fake_lap(1, 2, 9.8, 19.8))

        self.assertEqual(race.state, RaceState.WINNER_DECLARED)

        race.add_lap(make_fake_lap(2, 2, 10.0, 20.5))
        self.assertEqual(race.state, RaceState.FINISHED)

    def test_winner_mode_finishes_on_first_finisher(self):
        race = Race(previous_race=None)
        race.total_laps = 2
        race.race_end_mode = RaceEndMode.WINNER
        race.start(start_time=0.0)

        race.add_lap(make_fake_lap(1, 1, 10.0, 10.0))
        race.add_lap(make_fake_lap(2, 1, 10.5, 10.5))
        race.add_lap(make_fake_lap(1, 2, 9.8, 19.8))

        self.assertEqual(race.state, RaceState.FINISHED)

    def test_manual_mode_requires_manual_finish(self):
        race = Race(previous_race=None)
        race.total_laps = 2
        race.race_end_mode = RaceEndMode.MANUAL
        race.start(start_time=0.0)

        race.add_lap(make_fake_lap(1, 1, 10.0, 10.0))
        race.add_lap(make_fake_lap(1, 2, 9.8, 19.8))

        self.assertEqual(race.state, RaceState.RUNNING)


if __name__ == "__main__":
    unittest.main()
