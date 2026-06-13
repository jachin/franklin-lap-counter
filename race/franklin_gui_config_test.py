import json
import tempfile
import unittest
from pathlib import Path

from gui_config import load_initial_config, write_config
from race.race_mode import RaceMode
from race.race_state import RaceEndMode
from racer_colors import COLOR_SCHEMES


class TestLoadInitialConfig(unittest.TestCase):
    def test_missing_config_file_uses_defaults(self):
        missing_path = Path("/tmp/definitely_missing_franklin_config.json")
        race_mode, total_laps, race_end_mode, contestants, last_race_ids, color_map = (
            load_initial_config(missing_path)
        )

        self.assertEqual(race_mode, RaceMode.TRAINING)
        self.assertEqual(total_laps, 10)
        self.assertEqual(race_end_mode, RaceEndMode.LAST_CAR)
        self.assertEqual(contestants, [])
        self.assertEqual(last_race_ids, [])
        self.assertEqual(color_map, {})

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

            (
                race_mode,
                total_laps,
                race_end_mode,
                contestants,
                last_race_ids,
                color_map,
            ) = load_initial_config(config_path)

            self.assertEqual(race_mode, RaceMode.TRAINING)
            self.assertEqual(total_laps, 7)
            self.assertEqual(race_end_mode, RaceEndMode.LAST_CAR)
            self.assertEqual(contestants, [{"transmitter_id": 3, "name": "Alice"}])
            self.assertEqual(last_race_ids, [])
            self.assertEqual(color_map, {})

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

            (
                race_mode,
                total_laps,
                race_end_mode,
                contestants,
                last_race_ids,
                color_map,
            ) = load_initial_config(config_path)

            self.assertEqual(race_mode, RaceMode.TRAINING)
            self.assertEqual(total_laps, 10)
            self.assertEqual(race_end_mode, RaceEndMode.MANUAL)
            self.assertEqual(contestants, [{"transmitter_id": 8, "name": "Bob"}])
            self.assertEqual(last_race_ids, [])
            self.assertEqual(color_map, {})

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

            (
                race_mode,
                total_laps,
                race_end_mode,
                contestants,
                last_race_ids,
                color_map,
            ) = load_initial_config(config_path)

            self.assertEqual(race_mode, RaceMode.TRAINING)
            self.assertEqual(total_laps, 12)
            self.assertEqual(race_end_mode, RaceEndMode.LAST_CAR)
            self.assertEqual(contestants, [{"transmitter_id": 5, "name": "Cara"}])
            self.assertEqual(last_race_ids, [])
            self.assertEqual(color_map, {})

    def test_race_mode_accepts_human_friendly_and_legacy_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "franklin.config.json"

            config_path.write_text(json.dumps({"race_mode": "Fake Race Mode"}))
            (
                race_mode,
                _total_laps,
                _end_mode,
                _contestants,
                _last_race_ids,
                _color_map,
            ) = load_initial_config(config_path)
            self.assertEqual(race_mode, RaceMode.FAKE)

            # Delete the SQLite database to force a fresh migration from the new JSON config file
            db_path = config_path.parent / "franklin.db"
            if db_path.exists():
                db_path.unlink()

            config_path.write_text(json.dumps({"race_mode": "REAL"}))
            (
                race_mode,
                _total_laps,
                _end_mode,
                _contestants,
                _last_race_ids,
                _color_map,
            ) = load_initial_config(config_path)
            self.assertEqual(race_mode, RaceMode.REAL)

    def test_last_race_contestant_ids_are_loaded_and_sanitized(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "franklin.config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "last_race_contestant_ids": [5, "7", "oops", -1, 5, 0, 9],
                    }
                )
            )

            (
                _mode,
                _laps,
                _end_mode,
                _contestants,
                last_race_ids,
                _color_map,
            ) = load_initial_config(config_path)
            self.assertEqual(last_race_ids, [5, 7, 9])

    def test_racer_color_assignments_are_loaded_and_sanitized(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "franklin.config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "racer_color_assignments": {
                            "1": {"primary": "#112233", "secondary": "#445566"},
                            "3": {"primary": "#AABBCC", "secondary": "#DDEEFF"},
                            "bad": {"primary": "#001122", "secondary": "#334455"},
                            "-4": {"primary": "#102030", "secondary": "#405060"},
                            "8": {"primary": "bad", "secondary": "#778899"},
                            "9": {"primary": "#abcdef", "secondary": 123},
                            "10": 3,
                        }
                    }
                )
            )

            (
                _mode,
                _laps,
                _end_mode,
                _contestants,
                _last_race_ids,
                color_map,
            ) = load_initial_config(config_path)
            self.assertEqual(
                color_map,
                {
                    1: ("#112233", "#445566"),
                    3: ("#aabbcc", "#ddeeff"),
                    10: COLOR_SCHEMES[3],
                },
            )


class TestWriteConfig(unittest.TestCase):
    def test_write_config_round_trip_with_load_initial_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "nested" / "franklin.config.json"
            write_config(
                config_path,
                race_mode=RaceMode.REAL,
                total_laps=14,
                race_end_mode=RaceEndMode.MANUAL,
                contestants_data=[
                    {"transmitter_id": 2, "name": "Alice"},
                    {"transmitter_id": 7, "name": "Bob"},
                ],
                last_race_contestant_ids=[7, 2, 7, -1, 0],
                racer_color_assignments={
                    7: ("#778899", "#112233"),
                    2: ("#abcdef", "#fedcba"),
                },
            )

            db_path = config_path.parent / "franklin.db"
            self.assertTrue(db_path.exists())

            (
                race_mode,
                total_laps,
                race_end_mode,
                contestants,
                last_race_ids,
                color_map,
            ) = load_initial_config(config_path)

            self.assertEqual(race_mode, RaceMode.REAL)
            self.assertEqual(total_laps, 14)
            self.assertEqual(race_end_mode, RaceEndMode.MANUAL)
            self.assertEqual(
                contestants,
                [
                    {"transmitter_id": 2, "name": "Alice"},
                    {"transmitter_id": 7, "name": "Bob"},
                ],
            )
            self.assertEqual(last_race_ids, [2, 7])
            self.assertEqual(
                color_map,
                {
                    2: ("#abcdef", "#fedcba"),
                    7: ("#778899", "#112233"),
                },
            )

    def test_write_config_persists_expected_json_shape(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "franklin.config.json"
            write_config(
                config_path,
                race_mode=RaceMode.FAKE,
                total_laps=9,
                race_end_mode=RaceEndMode.LAST_CAR,
                contestants_data=[{"transmitter_id": 4, "name": "Cara"}],
                last_race_contestant_ids=[4],
                racer_color_assignments={4: ("#0a0b0c", "#0d0e0f")},
            )

            from database import LapDatabase

            db_path = config_path.parent / "franklin.db"
            db = LapDatabase(str(db_path))
            self.assertEqual(db.get_preference("race_mode"), RaceMode.FAKE.value)
            self.assertEqual(db.get_preference("total_laps"), 9)
            self.assertEqual(
                db.get_preference("race_end_mode"), RaceEndMode.LAST_CAR.value
            )
            self.assertEqual(
                db.get_preference("contestants"),
                [{"transmitter_id": 4, "name": "Cara"}],
            )
            self.assertEqual(db.get_preference("last_race_contestant_ids"), [4])
            self.assertEqual(
                db.get_preference("racer_color_assignments"),
                {"4": {"primary": "#0a0b0c", "secondary": "#0d0e0f"}},
            )
            db.close()


if __name__ == "__main__":
    unittest.main()
