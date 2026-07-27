import unittest

from driver_web_app import DriverWebAppServer


class TestDriverLeaderboardStats(unittest.TestCase):
    def test_lap_zero_is_excluded_from_best_and_last_lap(self):
        server = DriverWebAppServer(db_path=":memory:")
        self.addCleanup(server.db.close)
        race_id = server.db.create_race(start_at=100.0)
        server.db.add_lap(
            race_id, 1, 1, 0, 0.5, race_start_at=100.0, lap_at=100.5
        )
        server.db.add_lap(
            race_id, 1, 1, 1, 10.0, race_start_at=100.0, lap_at=110.5
        )
        server.db.add_lap(
            race_id, 1, 1, 2, 9.0, race_start_at=100.0, lap_at=119.5
        )

        summary = server._build_race_mode_summary(
            race_id=race_id, names_by_id={}, total_laps=3
        )
        row = summary["leaderboard"][0]

        self.assertEqual(row["lap_count"], 2)
        self.assertEqual(row["best_lap_time"], 9.0)
        self.assertEqual(row["last_lap_time"], 9.0)


if __name__ == "__main__":
    unittest.main()