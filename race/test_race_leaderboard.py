import unittest
from .race import Race
from .lap import Lap

class TestRaceLeaderboard(unittest.TestCase):
    def setUp(self):
        self.race = Race()

    def test_empty_leaderboard(self):
        leaderboard = self.race.leaderboard()
        self.assertEqual(leaderboard, [])

    def test_single_lap(self):
        lap = Lap(racer_id=1, lap_number=1, lap_time=10.0)
        self.race.add_fake_lap(lap)
        leaderboard = self.race.leaderboard()
        self.assertEqual(leaderboard, [(1, 1, 1, 10.0, 10.0)])

    def test_multiple_laps_single_racer(self):
        laps = [Lap(racer_id=1, lap_number=i, lap_time=10.0 + i) for i in range(1, 4)]
        for lap in laps:
            self.race.add_fake_lap(lap)
        leaderboard = self.race.leaderboard()
        expected = [(1, 1, 3, 11.0, 11.0+12.0+13.0)]
        self.assertEqual(leaderboard, expected)

    def test_multiple_racers_sort_by_laps(self):
        # Racer 1: 3 laps
        for i in range(1, 4):
            self.race.add_fake_lap(Lap(racer_id=1, lap_number=i, lap_time=10.0))
        # Racer 2: 2 laps
        for i in range(1, 3):
            self.race.add_fake_lap(Lap(racer_id=2, lap_number=i, lap_time=9.0))
        leaderboard = self.race.leaderboard()
        self.assertEqual(leaderboard[0][1], 1)  # Racer 1 first, more laps
        self.assertEqual(leaderboard[1][1], 2)  # Racer 2 second

    def test_tie_on_laps_sort_by_best_lap_time(self):
        # Racer 1: 2 laps, best lap 9.0
        self.race.add_fake_lap(Lap(racer_id=1, lap_number=1, lap_time=10.0))
        self.race.add_fake_lap(Lap(racer_id=1, lap_number=2, lap_time=9.0))

        # Racer 2: 2 laps, best lap 8.0
        self.race.add_fake_lap(Lap(racer_id=2, lap_number=1, lap_time=10.0))
        self.race.add_fake_lap(Lap(racer_id=2, lap_number=2, lap_time=8.0))

        leaderboard = self.race.leaderboard()
        # Racer 2 should be before racer 1 because of better best lap time
        self.assertEqual(leaderboard[0][1], 2)
        self.assertEqual(leaderboard[1][1], 1)

    def test_positions_are_correct(self):
        # 3 racers with different laps and times
        # Racer 1: 3 laps, best lap 10.0
        self.race.add_fake_lap(Lap(racer_id=1, lap_number=1, lap_time=10.0))
        self.race.add_fake_lap(Lap(racer_id=1, lap_number=2, lap_time=11.0))
        self.race.add_fake_lap(Lap(racer_id=1, lap_number=3, lap_time=12.0))
        # Racer 2: 3 laps, best lap 9.5
        self.race.add_fake_lap(Lap(racer_id=2, lap_number=1, lap_time=10.0))
        self.race.add_fake_lap(Lap(racer_id=2, lap_number=2, lap_time=9.5))
        self.race.add_fake_lap(Lap(racer_id=2, lap_number=3, lap_time=11.0))
        # Racer 3: 2 laps, best lap 9.0
        self.race.add_fake_lap(Lap(racer_id=3, lap_number=1, lap_time=9.0))
        self.race.add_fake_lap(Lap(racer_id=3, lap_number=2, lap_time=9.2))

        leaderboard = self.race.leaderboard()
        # Racer 2 should be in position 1 (best lap is lower)
        self.assertEqual(leaderboard[0][0], 1)  # Position
        self.assertEqual(leaderboard[0][1], 2)  # Racer ID
        # Racer 1 in position 2
        self.assertEqual(leaderboard[1][0], 2)
        self.assertEqual(leaderboard[1][1], 1)
        # Racer 3 in position 3 (less laps)
        self.assertEqual(leaderboard[2][0], 3)
        self.assertEqual(leaderboard[2][1], 3)


if __name__ == "__main__":
    unittest.main()
