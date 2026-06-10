import unittest

from race.lap import EpochSeconds, Lap, LapTime
from race.race import Race


def rel_lap(
    racer_id: int,
    lap_number: int,
    seconds_from_start: float,
    lap_time: float,
) -> Lap:
    synthetic_start_epoch = 1.0
    return Lap(
        racer_id=racer_id,
        lap_number=lap_number,
        race_start_at=EpochSeconds(synthetic_start_epoch),
        lap_at=EpochSeconds(synthetic_start_epoch + seconds_from_start),
        recorded_at=EpochSeconds(synthetic_start_epoch + seconds_from_start),
        lap_time=LapTime(lap_time),
    )


class TestRaceLeaderboard(unittest.TestCase):
    def setUp(self):
        self.race = Race()

    def test_empty_leaderboard(self):
        leaderboard = self.race.leaderboard()
        self.assertEqual(leaderboard, [])

    def test_single_lap(self):
        self.race.add_fake_lap(rel_lap(1, 0, 0.5, 0.5))
        self.race.add_fake_lap(rel_lap(1, 1, 10.0, 10.0))

        leaderboard = self.race.leaderboard()
        self.assertEqual(leaderboard, [(1, 1, 1, 10.0, 10.0, 10.0)])

    def test_multiple_laps_single_racer(self):
        self.race.add_fake_lap(rel_lap(1, 0, 0.5, 0.5))

        laps = [
            rel_lap(1, 1, 9.0, 9.0),
            rel_lap(1, 2, 19.0, 10.0),
            rel_lap(1, 3, 30.0, 11.0),
        ]
        for lap in laps:
            self.race.add_fake_lap(lap)

        leaderboard = self.race.leaderboard()
        expected = [(1, 1, 3, 9.0, 11.0, 30.0)]
        self.assertEqual(leaderboard, expected)

    def test_multiple_racers_sort_by_laps(self):
        self.race.add_fake_lap(rel_lap(1, 0, 0.5, 0.5))
        self.race.add_fake_lap(rel_lap(2, 0, 0.5, 0.5))

        for i in range(1, 4):
            self.race.add_fake_lap(rel_lap(1, i, 10.0 + i, 10.0))
        for i in range(1, 3):
            self.race.add_fake_lap(rel_lap(2, i, 9.0 + i, 9.0))

        leaderboard = self.race.leaderboard()
        self.assertEqual(leaderboard[0][1], 1)
        self.assertEqual(leaderboard[1][1], 2)

    def test_tie_on_laps_sort_by_best_lap_time(self):
        self.race.add_fake_lap(rel_lap(1, 0, 0.5, 0.5))
        self.race.add_fake_lap(rel_lap(2, 0, 0.5, 0.5))

        self.race.add_fake_lap(rel_lap(1, 1, 10.0, 10.0))
        self.race.add_fake_lap(rel_lap(1, 2, 19.0, 9.0))

        self.race.add_fake_lap(rel_lap(2, 1, 10.0, 10.0))
        self.race.add_fake_lap(rel_lap(2, 2, 8.0, 8.0))

        leaderboard = self.race.leaderboard()
        self.assertEqual(leaderboard[0][1], 2)
        self.assertEqual(leaderboard[1][1], 1)

    def test_lap_zero_start_is_not_counted(self):
        self.race.add_fake_lap(rel_lap(1, 0, 0.5, 0.5))
        self.race.add_fake_lap(rel_lap(1, 1, 10.0, 9.5))

        self.race.add_fake_lap(rel_lap(2, 0, 0.7, 0.7))
        self.race.add_fake_lap(rel_lap(2, 1, 9.0, 8.3))

        leaderboard = self.race.leaderboard()
        self.assertEqual(leaderboard[0][2], 1)
        self.assertEqual(leaderboard[1][2], 1)
        self.assertEqual(leaderboard[0][1], 2)
        self.assertEqual(leaderboard[1][1], 1)

    def test_positions_are_correct(self):
        self.race.add_fake_lap(rel_lap(1, 1, 10.0, 10.0))
        self.race.add_fake_lap(rel_lap(1, 2, 11.0, 11.0))
        self.race.add_fake_lap(rel_lap(1, 3, 12.0, 12.0))

        self.race.add_fake_lap(rel_lap(2, 1, 10.0, 10.0))
        self.race.add_fake_lap(rel_lap(2, 2, 9.5, 9.5))
        self.race.add_fake_lap(rel_lap(2, 3, 11.0, 11.0))

        self.race.add_fake_lap(rel_lap(3, 1, 9.0, 9.0))
        self.race.add_fake_lap(rel_lap(3, 2, 9.2, 9.2))

        leaderboard = self.race.leaderboard()
        self.assertEqual(leaderboard[0][0], 1)
        self.assertEqual(leaderboard[0][1], 2)
        self.assertEqual(leaderboard[1][0], 2)
        self.assertEqual(leaderboard[1][1], 1)
        self.assertEqual(leaderboard[2][0], 3)
        self.assertEqual(leaderboard[2][1], 3)

    def test_previous_race_contestants_are_visible_with_zeroed_stats(self):
        previous_race = Race()
        previous_race.add_fake_lap(rel_lap(11, 1, 10.0, 10.0))
        previous_race.add_fake_lap(rel_lap(12, 1, 11.0, 11.0))

        next_race = Race(previous_race=previous_race)
        leaderboard = next_race.leaderboard()

        self.assertEqual(len(leaderboard), 2)
        racer_ids = {row[1] for row in leaderboard}
        self.assertEqual(racer_ids, {11, 12})
        for _pos, _racer_id, lap_count, best, last, total in leaderboard:
            self.assertEqual(lap_count, 0)
            self.assertEqual(best, float("inf"))
            self.assertEqual(last, float("inf"))
            self.assertEqual(total, 0.0)


if __name__ == "__main__":
    unittest.main()
