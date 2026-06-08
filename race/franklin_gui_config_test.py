import json
import tempfile
import unittest
from pathlib import Path

from gui_config import load_initial_config
from race.race_state import RaceEndMode


class TestLoadInitialConfig(unittest.TestCase):
    def test_missing_config_file_uses_defaults(self):
        missing_path = Path("/tmp/definitely_missing_franklin_config.json")
        total_laps, race_end_mode, contestants = load_initial_config(missing_path)

        self.assertEqual(total_laps, 10)
        self.assertEqual(race_end_mode, RaceEndMode.LAST_CAR)
        self.assertEqual(contestants, [])

    def test_missing_race_end_mode_does_not_block_startup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "franklin.config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "total_laps": 7,
                        # old config without race_end_mode
                        "contestants": [{"transmitter_id": 3, "name": "Alice"}],
                    }
                )
            )

            total_laps, race_end_mode, contestants = load_initial_config(config_path)

            self.assertEqual(total_laps, 7)
            self.assertEqual(race_end_mode, RaceEndMode.LAST_CAR)
            self.assertEqual(contestants, [{"transmitter_id": 3, "name": "Alice"}])

    def test_invalid_total_laps_preserves_other_preferences(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "franklin.config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "total_laps": "oops",
                        "race_end_mode": "manual",
                        "contestants": [{"transmitter_id": 8, "name": "Bob"}],
                    }
                )
            )

            total_laps, race_end_mode, contestants = load_initial_config(config_path)

            self.assertEqual(total_laps, 10)
            self.assertEqual(race_end_mode, RaceEndMode.MANUAL)
            self.assertEqual(contestants, [{"transmitter_id": 8, "name": "Bob"}])

    def test_invalid_race_end_mode_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "franklin.config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "total_laps": 12,
                        "race_end_mode": "unknown_mode",
                        "contestants": [{"transmitter_id": 5, "name": "Cara"}],
                    }
                )
            )

            total_laps, race_end_mode, contestants = load_initial_config(config_path)

            self.assertEqual(total_laps, 12)
            self.assertEqual(race_end_mode, RaceEndMode.LAST_CAR)
            self.assertEqual(contestants, [{"transmitter_id": 5, "name": "Cara"}])


if __name__ == "__main__":
    unittest.main()
