import json
import tempfile
import unittest
from pathlib import Path

from database import LapDatabase
from race.race_engine import RaceEngine
from race.race_mode import RaceMode
from race.race_state import RaceEndMode, RaceState

START_AT = 1000.0


class RaceEngineTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "test.db")
        self.db = LapDatabase(self.db_path)
        self.engine = RaceEngine(self.db, auto_resume=False)

    def tearDown(self) -> None:
        self.db.close()
        self._tmpdir.cleanup()

    def lap(self, racer_id: int, lap_at: float, *, start_at: float = START_AT):
        return self.engine.record_lap(
            racer_id=racer_id,
            race_start_at=start_at,
            lap_at=lap_at,
            recorded_at=lap_at,
        )

    def complete_laps(self, racer_id: int, count: int, *, spacing: float = 5.0):
        """Feed `count` scored laps (plus the lap-0 start trigger) for a racer."""
        # First event is the lap-0 start trigger; the next `count` are scored.
        self.lap(racer_id, START_AT + 0.5 + racer_id * 0.01)
        for n in range(1, count + 1):
            self.lap(racer_id, START_AT + n * spacing + racer_id * 0.01)


class TestStartAndPersist(RaceEngineTestBase):
    def test_start_creates_race_with_config_notes(self):
        self.engine.start(
            start_at=START_AT,
            race_mode=RaceMode.REAL,
            total_laps=5,
            race_end_mode=RaceEndMode.LAST_CAR,
        )
        self.assertIsNotNone(self.engine.current_race_id)
        self.assertEqual(self.engine.race.state, RaceState.RUNNING)

        row = self.db.get_in_progress_race()
        assert row is not None
        notes = json.loads(row["notes"])
        self.assertEqual(notes["total_laps"], 5)
        self.assertEqual(notes["end_mode"], "last_car")
        self.assertEqual(notes["mode"], RaceMode.REAL.value)

    def test_training_mode_maps_to_unlimited_manual(self):
        self.engine.start(
            start_at=START_AT,
            race_mode=RaceMode.TRAINING,
            total_laps=5,
            race_end_mode=RaceEndMode.LAST_CAR,
        )
        self.assertEqual(self.engine.race.race_end_mode, RaceEndMode.MANUAL)
        self.assertGreater(self.engine.race.total_laps, 1000)


class TestLapIngestion(RaceEngineTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.engine.start(
            start_at=START_AT,
            race_mode=RaceMode.REAL,
            total_laps=10,
            race_end_mode=RaceEndMode.LAST_CAR,
        )

    def test_lap_recorded_and_persisted(self):
        self.lap(1, START_AT + 0.5)  # lap 0
        result = self.lap(1, START_AT + 5.0)  # lap 1
        self.assertTrue(result.changed)
        self.assertEqual(result.lap_number, 1)

        race_id = self.engine.current_race_id
        assert race_id is not None
        db_laps = self.db.get_race_laps(race_id)
        self.assertEqual(len(db_laps), 2)

    def test_duplicate_lap_ignored(self):
        self.lap(1, START_AT + 0.5)
        first = self.lap(1, START_AT + 5.0)
        duplicate = self.lap(1, START_AT + 5.0)
        self.assertTrue(first.changed)
        self.assertFalse(duplicate.changed)
        self.assertEqual(duplicate.note, "duplicate")

    def test_ingest_via_message_dict(self):
        msg = {
            "type": "lap",
            "racer_id": 2,
            "sensor_id": 2,
            "race_start_at": START_AT,
            "lap_at": START_AT + 3.0,
            "recorded_at": START_AT + 3.0,
        }
        result = self.engine.ingest(msg)
        self.assertTrue(result.changed)

    def test_lap_ignored_when_not_running(self):
        self.engine.end_race(at=START_AT + 100)
        result = self.lap(1, START_AT + 5.0)
        self.assertFalse(result.changed)
        self.assertEqual(result.note, "race_not_running")


class TestAutoFinish(RaceEngineTestBase):
    def test_winner_mode_finishes_when_leader_reaches_target(self):
        self.engine.start(
            start_at=START_AT,
            race_mode=RaceMode.REAL,
            total_laps=3,
            race_end_mode=RaceEndMode.WINNER,
        )
        self.lap(1, START_AT + 0.5)  # lap 0
        self.lap(1, START_AT + 5.0)  # lap 1
        self.lap(1, START_AT + 10.0)  # lap 2
        result = self.lap(1, START_AT + 15.0)  # lap 3 -> finish
        self.assertTrue(result.finished_now)
        self.assertEqual(self.engine.race.state, RaceState.FINISHED)
        self.assertIsNotNone(self.engine._end_at_epoch)

    def test_disqualify_unblocks_last_car_finish(self):
        self.engine.start(
            start_at=START_AT,
            race_mode=RaceMode.REAL,
            total_laps=2,
            race_end_mode=RaceEndMode.LAST_CAR,
        )
        # Racer 2 gets on the board first (one scored lap) so it counts as an
        # active contestant before the leader finishes.
        self.lap(1, START_AT + 0.5)  # racer 1 lap 0
        self.lap(2, START_AT + 0.6)  # racer 2 lap 0
        self.lap(2, START_AT + 6.0)  # racer 2 lap 1
        # Racer 1 finishes both laps -> WINNER_DECLARED (waiting on racer 2).
        self.lap(1, START_AT + 5.0)  # racer 1 lap 1
        self.lap(1, START_AT + 10.0)  # racer 1 lap 2
        self.assertEqual(self.engine.race.state, RaceState.WINNER_DECLARED)
        # Disqualifying the straggler should let the race finish.
        result = self.engine.disqualify(2)
        self.assertTrue(result.finished_now)
        self.assertEqual(self.engine.race.state, RaceState.FINISHED)


class TestRefereeAdjustments(RaceEngineTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.engine.start(
            start_at=START_AT,
            race_mode=RaceMode.REAL,
            total_laps=10,
            race_end_mode=RaceEndMode.LAST_CAR,
        )

    def test_penalty_changes_leaderboard_order(self):
        # Racer 1 is slightly faster than racer 2 (lower total time).
        self.lap(1, START_AT + 0.5)
        self.lap(1, START_AT + 10.0)
        self.lap(1, START_AT + 20.0)  # total ~20s
        self.lap(2, START_AT + 0.6)
        self.lap(2, START_AT + 10.5)
        self.lap(2, START_AT + 21.0)  # total ~21s

        order_before = [row[1] for row in self.engine.adjusted_leaderboard()]
        self.assertEqual(order_before[0], 1)

        self.engine.add_penalty(1, 5)  # pushes racer 1 behind racer 2
        order_after = [row[1] for row in self.engine.adjusted_leaderboard()]
        self.assertEqual(order_after[0], 2)

    def test_remove_latest_lap(self):
        self.lap(1, START_AT + 0.5)
        self.lap(1, START_AT + 5.0)
        self.lap(1, START_AT + 10.0)  # lap 2
        result = self.engine.remove_lap(1)
        self.assertTrue(result.changed)
        # Only the lap-0 trigger and lap 1 remain.
        scored = [lap for lap in self.engine.race.laps if lap.racer_id == 1]
        self.assertEqual(max(lap.lap_number for lap in scored), 1)

    def test_remove_specific_lap(self):
        self.lap(1, START_AT + 0.5)
        self.lap(1, START_AT + 5.0)  # lap 1
        self.lap(1, START_AT + 10.0)  # lap 2
        result = self.engine.remove_lap(1, lap_number=1)
        self.assertTrue(result.changed)
        remaining = sorted(
            lap.lap_number for lap in self.engine.race.laps if lap.racer_id == 1
        )
        self.assertEqual(remaining, [0, 2])

    def test_remove_missing_lap(self):
        result = self.engine.remove_lap(99)
        self.assertFalse(result.changed)
        self.assertEqual(result.note, "lap_not_found")

    def test_disqualified_racer_laps_ignored(self):
        self.engine.disqualify(3)
        result = self.lap(3, START_AT + 5.0)
        self.assertFalse(result.changed)
        self.assertEqual(result.note, "disqualified")


class TestReset(RaceEngineTestBase):
    def test_reset_clears_race(self):
        self.engine.start(
            start_at=START_AT,
            race_mode=RaceMode.REAL,
            total_laps=10,
            race_end_mode=RaceEndMode.LAST_CAR,
        )
        self.lap(1, START_AT + 0.5)
        self.lap(1, START_AT + 5.0)
        self.engine.add_penalty(1, 5)

        self.engine.reset()
        self.assertIsNone(self.engine.current_race_id)
        self.assertEqual(self.engine.race.state, RaceState.NOT_STARTED)
        self.assertEqual(self.engine.racer_penalties_seconds, {})
        self.assertEqual(self.engine.race.laps, [])


class TestResume(RaceEngineTestBase):
    def test_resume_rebuilds_race_from_db(self):
        self.engine.start(
            start_at=START_AT,
            race_mode=RaceMode.REAL,
            total_laps=10,
            race_end_mode=RaceEndMode.LAST_CAR,
        )
        self.complete_laps(1, 2)
        self.complete_laps(2, 1)
        self.engine.add_penalty(2, 5)
        # Persist a penalty audit row so resume can recover it.
        self.db.add_race_control_action(
            command="add_penalty",
            accepted=True,
            payload={"racer_id": 2, "penalty_seconds": 5},
            race_id=self.engine.current_race_id,
        )

        resumed = RaceEngine(self.db, auto_resume=True)
        self.assertEqual(resumed.race.state, RaceState.RUNNING)
        self.assertEqual(resumed.total_laps, 10)
        self.assertEqual(resumed.race_end_mode, RaceEndMode.LAST_CAR)
        self.assertEqual(resumed.racer_penalties_seconds.get(2), 5)
        # Racer 1 had 2 scored laps.
        scored = [
            lap for lap in resumed.race.laps if lap.racer_id == 1 and lap.lap_number > 0
        ]
        self.assertEqual(len(scored), 2)

    def test_resume_parses_legacy_notes(self):
        race_id = self.db.create_race(
            notes="Mode: Real Race Mode, Total Laps: 7", start_at=START_AT
        )
        self.db.add_lap(
            race_id=race_id,
            racer_id=1,
            sensor_id=1,
            lap_number=1,
            lap_time=5.0,
            race_start_at=START_AT,
            lap_at=START_AT + 5.0,
            recorded_at=START_AT + 5.0,
        )
        resumed = RaceEngine(self.db, auto_resume=True)
        self.assertEqual(resumed.total_laps, 7)
        self.assertEqual(resumed.race_mode, RaceMode.REAL)


class TestSnapshot(RaceEngineTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.engine.start(
            start_at=START_AT,
            race_mode=RaceMode.REAL,
            total_laps=10,
            race_end_mode=RaceEndMode.LAST_CAR,
        )

    def test_snapshot_is_json_serializable_without_infinity(self):
        # A racer with only the lap-0 trigger has inf best/last lap times.
        self.lap(1, START_AT + 0.5)
        self.lap(2, START_AT + 0.6)
        self.lap(2, START_AT + 5.0)  # racer 2 has a scored lap

        snapshot = self.engine.build_snapshot(snapshot_seq=1)
        encoded = json.dumps(snapshot)  # raises if inf/nan present? no -> check text
        self.assertNotIn("Infinity", encoded)
        self.assertNotIn("NaN", encoded)
        for row in snapshot["leaderboard"]:
            self.assertNotEqual(row["best_lap_time"], float("inf"))

    def test_snapshot_shape(self):
        self.lap(1, START_AT + 0.5)
        self.lap(1, START_AT + 5.0)
        snapshot = self.engine.build_snapshot(snapshot_seq=42)
        self.assertEqual(snapshot["schema_version"], 1)
        self.assertEqual(snapshot["snapshot_seq"], 42)
        self.assertEqual(snapshot["state"], "running")
        self.assertEqual(snapshot["start_at"], START_AT)
        self.assertEqual(snapshot["race_id"], self.engine.current_race_id)
        self.assertIn("leaderboard", snapshot)
        self.assertIn("laps", snapshot)

    def test_finished_snapshot_freezes_elapsed(self):
        self.lap(1, START_AT + 0.5)
        self.lap(1, START_AT + 5.0)
        self.engine.end_race(at=START_AT + 30.0)

        first = self.engine.build_snapshot(snapshot_seq=1)
        second = self.engine.build_snapshot(snapshot_seq=2)
        self.assertEqual(first["elapsed_seconds"], 30.0)
        self.assertEqual(first["elapsed_seconds"], second["elapsed_seconds"])
        self.assertEqual(first["state"], "finished")


if __name__ == "__main__":
    unittest.main()
