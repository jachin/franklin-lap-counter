import unittest
from race.race import make_fake_lap
from race.lap import Lap

class TestMakeFakeLap(unittest.TestCase):
    def test_make_fake_lap_basic(self):
        """Test make_fake_lap with basic parameters."""
        racer_id = 1
        lap_number = 1
        lap_time = 10.5
        seconds_from_start = 10.5

        lap = make_fake_lap(racer_id, lap_number, lap_time, seconds_from_start)

        self.assertEqual(lap.racer_id, racer_id)
        self.assertEqual(lap.lap_number, lap_number)
        self.assertEqual(lap.lap_time, lap_time)
        self.assertEqual(lap.internal_lap_time, lap_time)
        self.assertEqual(lap.seconds_from_race_start, seconds_from_start)

        # Check that the returned object is a Lap instance
        self.assertIsInstance(lap, Lap)
        # For NewType annotations, we can only check if the underlying type is float
        self.assertIsInstance(lap.seconds_from_race_start, float)
        self.assertIsInstance(lap.internal_lap_time, float)
        self.assertIsInstance(lap.lap_time, float)

    def test_make_fake_lap_with_explicit_seconds_from_start(self):
        """Test make_fake_lap with explicit seconds_from_start value."""
        racer_id = 2
        lap_number = 3
        lap_time = 9.8
        seconds_from_start = 25.4

        lap = make_fake_lap(racer_id, lap_number, lap_time, seconds_from_start)

        self.assertEqual(lap.racer_id, racer_id)
        self.assertEqual(lap.lap_number, lap_number)
        self.assertEqual(lap.lap_time, lap_time)
        self.assertEqual(lap.internal_lap_time, lap_time)
        self.assertEqual(lap.seconds_from_race_start, seconds_from_start)

    def test_make_fake_lap_for_start_trigger(self):
        """Test make_fake_lap for lap number 0 (start trigger)."""
        racer_id = 4
        lap_number = 0  # Start trigger
        lap_time = 1.5
        seconds_from_start = 1.5

        lap = make_fake_lap(racer_id, lap_number, lap_time, seconds_from_start)

        self.assertEqual(lap.racer_id, racer_id)
        self.assertEqual(lap.lap_number, lap_number)
        self.assertEqual(lap.lap_time, lap_time)
        self.assertEqual(lap.internal_lap_time, lap_time)
        self.assertEqual(lap.seconds_from_race_start, seconds_from_start)

        # Start trigger should have lap_number of 0
        self.assertEqual(lap.lap_number, 0)


if __name__ == "__main__":
    unittest.main()
