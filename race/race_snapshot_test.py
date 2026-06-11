import tempfile
import unittest
from pathlib import Path

from database import LapDatabase
from race.race_engine import RaceEngine
from race.race_mode import RaceMode
from race.race_snapshot import RaceSnapshot, idle_snapshot
from race.race_state import RaceEndMode

START_AT = 1000.0


class TestRaceSnapshotParsing(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = str(Path(self._tmpdir.name) / "test.db")
        self.db = LapDatabase(db_path)
        self.engine = RaceEngine(self.db, auto_resume=False)

    def tearDown(self) -> None:
        self.db.close()
        self._tmpdir.cleanup()

    def _running_snapshot(self) -> RaceSnapshot:
        self.engine.start(
            start_at=START_AT,
            race_mode=RaceMode.REAL,
            total_laps=5,
            race_end_mode=RaceEndMode.LAST_CAR,
        )
        self.engine.record_lap(racer_id=1, race_start_at=START_AT, lap_at=START_AT + 0.5)
        self.engine.record_lap(racer_id=1, race_start_at=START_AT, lap_at=START_AT + 5.0)
        self.engine.add_penalty(2, 5)
        data = self.engine.build_snapshot(snapshot_seq=7)
        return RaceSnapshot.from_dict(data)

    def test_roundtrip_from_engine(self):
        snap = self._running_snapshot()
        self.assertEqual(snap.schema_version, 1)
        self.assertEqual(snap.snapshot_seq, 7)
        self.assertEqual(snap.state, "running")
        self.assertEqual(snap.race_mode, RaceMode.REAL.value)
        self.assertEqual(snap.effective_total_laps, 5)
        self.assertTrue(snap.is_going)
        self.assertEqual(snap.penalties.get(2), 5)
        self.assertTrue(any(row.racer_id == 1 for row in snap.leaderboard))

    def test_clock_advances_while_running(self):
        snap = self._running_snapshot()
        base = snap.received_monotonic
        first = snap.current_elapsed(now_monotonic=base)
        later = snap.current_elapsed(now_monotonic=base + 3.0)
        self.assertAlmostEqual(later - first, 3.0, places=3)

    def test_clock_frozen_when_finished(self):
        self.engine.start(
            start_at=START_AT,
            race_mode=RaceMode.REAL,
            total_laps=5,
            race_end_mode=RaceEndMode.LAST_CAR,
        )
        self.engine.end_race(at=START_AT + 42.0)
        snap = RaceSnapshot.from_dict(self.engine.build_snapshot(snapshot_seq=1))
        self.assertEqual(snap.state, "finished")
        self.assertEqual(snap.current_elapsed(now_monotonic=snap.received_monotonic), 42.0)
        self.assertEqual(
            snap.current_elapsed(now_monotonic=snap.received_monotonic + 10.0), 42.0
        )

    def test_supersedes(self):
        older = RaceSnapshot.from_dict(self.engine.build_snapshot(snapshot_seq=1))
        newer = RaceSnapshot.from_dict(self.engine.build_snapshot(snapshot_seq=2))
        self.assertTrue(newer.supersedes(older))
        self.assertFalse(older.supersedes(newer))
        self.assertTrue(older.supersedes(None))

    def test_idle_snapshot(self):
        snap = idle_snapshot()
        self.assertEqual(snap.state, "not_started")
        self.assertFalse(snap.is_going)
        self.assertEqual(snap.leaderboard, [])
        self.assertEqual(snap.current_elapsed(), 0.0)

    def test_dq_row_parsed(self):
        self.engine.start(
            start_at=START_AT,
            race_mode=RaceMode.REAL,
            total_laps=5,
            race_end_mode=RaceEndMode.LAST_CAR,
        )
        self.engine.record_lap(racer_id=3, race_start_at=START_AT, lap_at=START_AT + 0.5)
        self.engine.record_lap(racer_id=3, race_start_at=START_AT, lap_at=START_AT + 4.0)
        self.engine.disqualify(3)
        snap = RaceSnapshot.from_dict(self.engine.build_snapshot(snapshot_seq=1))
        dq_rows = [row for row in snap.leaderboard if row.disqualified]
        self.assertEqual(len(dq_rows), 1)
        self.assertEqual(dq_rows[0].racer_id, 3)
        self.assertIsNone(dq_rows[0].position)


if __name__ == "__main__":
    unittest.main()
