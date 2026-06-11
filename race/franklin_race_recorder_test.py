import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from race.race_mode import RaceMode
from race.race_state import RaceEndMode, RaceState


ROOT = Path(__file__).resolve().parents[1]
RECORDER_PATH = ROOT / "franklin-race-recorder.py"
spec = importlib.util.spec_from_file_location("franklin_race_recorder", RECORDER_PATH)
assert spec is not None
recorder_module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(recorder_module)

RaceRecorder = recorder_module.RaceRecorder

START_AT = 1000.0


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.published: list[tuple[str, str]] = []

    def set(self, key: str, value: str) -> bool:
        self.values[key] = value
        return True

    def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return 1


class RaceRecorderTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "test.db")
        with patch.object(recorder_module.redis, "Redis", return_value=MagicMock()):
            self.recorder = RaceRecorder(
                redis_socket="unused.sock", db_path=self.db_path, persist=True
            )
        self.redis = FakeRedis()
        self.recorder.redis = self.redis

    def tearDown(self) -> None:
        self.recorder.db.close()
        self._tmpdir.cleanup()

    def send_start_command(
        self,
        *,
        command_id: str = "cmd-1",
        total_laps: int = 3,
        race_end_mode: RaceEndMode = RaceEndMode.LAST_CAR,
        race_mode: RaceMode = RaceMode.REAL,
    ) -> None:
        self.recorder._handle(
            recorder_module.HARDWARE_IN_CHANNEL,
            json.dumps(
                {
                    "type": "command",
                    "command": "start_race",
                    "command_id": command_id,
                    "source": "test",
                    "race_mode": race_mode.value,
                    "total_laps": total_laps,
                    "race_end_mode": race_end_mode.value,
                    "start_at": START_AT,
                }
            ),
        )

    def send_start_event(self, *, command_id: str = "cmd-1") -> None:
        self.recorder._handle(
            recorder_module.HARDWARE_OUT_CHANNEL,
            json.dumps({"type": "start_race", "command_id": command_id, "at": START_AT}),
        )

    def send_lap(self, racer_id: int, at_offset: float) -> None:
        lap_at = START_AT + at_offset
        self.recorder._handle(
            recorder_module.HARDWARE_OUT_CHANNEL,
            json.dumps(
                {
                    "type": "lap",
                    "racer_id": racer_id,
                    "sensor_id": racer_id,
                    "race_start_at": START_AT,
                    "lap_at": lap_at,
                    "recorded_at": lap_at,
                }
            ),
        )

    def snapshots(self) -> list[dict[str, object]]:
        return [
            json.loads(payload)
            for channel, payload in self.redis.published
            if channel == recorder_module.RACE_STATE_CHANNEL
        ]

    def end_race_commands(self) -> list[dict[str, object]]:
        return [
            json.loads(payload)
            for channel, payload in self.redis.published
            if channel == recorder_module.HARDWARE_IN_CHANNEL
        ]


class TestRaceRecorderRedisScenarios(RaceRecorderTestBase):
    def test_future_start_event_waits_until_scheduled_go_time(self):
        self.send_start_command(total_laps=5, race_end_mode=RaceEndMode.WINNER)
        start_at = 2000.0

        with patch.object(recorder_module.time, "time", return_value=start_at - 2.0):
            self.recorder._handle(
                recorder_module.HARDWARE_OUT_CHANNEL,
                json.dumps(
                    {"type": "start_race", "command_id": "cmd-1", "at": start_at}
                ),
            )

        self.assertEqual(self.recorder.engine.race.state, RaceState.NOT_STARTED)
        self.assertEqual(self.snapshots(), [])

        self.recorder._process_pending_start(start_at - 0.1)
        self.assertEqual(self.recorder.engine.race.state, RaceState.NOT_STARTED)

        self.recorder._process_pending_start(start_at)

        self.assertEqual(self.recorder.engine.race.state, RaceState.RUNNING)
        self.assertEqual(self.snapshots()[-1]["start_at"], start_at)

    def test_start_command_config_is_applied_when_hardware_start_echo_arrives(self):
        self.send_start_command(total_laps=5, race_end_mode=RaceEndMode.WINNER)
        self.send_start_event()

        self.assertEqual(self.recorder.engine.race.state, RaceState.RUNNING)
        self.assertEqual(self.recorder.engine.race.total_laps, 5)
        self.assertEqual(self.recorder.engine.race.race_end_mode, RaceEndMode.WINNER)

        snapshot = self.snapshots()[-1]
        self.assertEqual(snapshot["state"], "running")
        self.assertEqual(snapshot["total_laps"], 5)
        self.assertEqual(snapshot["race_end_mode"], RaceEndMode.WINNER.value)
        self.assertEqual(self.redis.values[recorder_module.RACE_STATE_LATEST_KEY], json.dumps(snapshot))

    def test_start_command_fallback_starts_race_without_hardware_echo(self):
        self.send_start_command(
            total_laps=5,
            race_end_mode=RaceEndMode.WINNER,
            race_mode=RaceMode.TRAINING,
        )

        self.recorder._process_pending_command_start(START_AT - 0.1)
        self.assertEqual(self.recorder.engine.race.state, RaceState.NOT_STARTED)

        self.recorder._process_pending_command_start(START_AT)

        self.assertEqual(self.recorder.engine.race.state, RaceState.RUNNING)
        snapshot = self.snapshots()[-1]
        self.assertEqual(snapshot["state"], "running")
        self.assertEqual(snapshot["race_mode"], RaceMode.TRAINING.value)
        self.assertEqual(snapshot["start_at"], START_AT)

    def test_shutdown_finishes_running_race_and_publishes_snapshot(self):
        self.send_start_command(total_laps=5, race_end_mode=RaceEndMode.MANUAL)
        self.send_start_event()

        self.recorder._finish_running_race_on_shutdown()

        self.assertEqual(self.recorder.engine.race.state, RaceState.FINISHED)
        self.assertEqual(self.snapshots()[-1]["state"], "finished")

    def test_winner_mode_publishes_end_race_once_when_leader_hits_target(self):
        self.send_start_command(total_laps=2, race_end_mode=RaceEndMode.WINNER)
        self.send_start_event()

        self.send_lap(1, 0.5)  # lap 0/start trigger
        self.send_lap(1, 5.0)  # lap 1
        self.send_lap(1, 10.0)  # lap 2 -> finish

        self.assertEqual(self.recorder.engine.race.state, RaceState.FINISHED)
        end_commands = self.end_race_commands()
        self.assertEqual(len(end_commands), 1)
        self.assertEqual(end_commands[0]["command"], "end_race")
        self.assertEqual(end_commands[0]["source"], recorder_module.SOURCE)

        snapshot = self.snapshots()[-1]
        self.assertEqual(snapshot["state"], "finished")
        leaderboard = snapshot["leaderboard"]
        assert isinstance(leaderboard, list)
        self.assertEqual(leaderboard[0]["racer_id"], 1)
        self.assertEqual(leaderboard[0]["lap_count"], 2)

    def test_last_car_penalty_and_disqualification_update_finish_and_leaderboard(self):
        self.send_start_command(total_laps=2, race_end_mode=RaceEndMode.LAST_CAR)
        self.send_start_event()

        self.send_lap(1, 0.5)
        self.send_lap(2, 0.6)
        self.send_lap(2, 5.5)
        self.send_lap(1, 5.0)
        self.send_lap(1, 10.0)
        self.assertEqual(self.recorder.engine.race.state, RaceState.WINNER_DECLARED)

        self.recorder._handle(
            recorder_module.EVENTS_CHANNEL,
            json.dumps(
                {
                    "type": "race_control",
                    "command": "add_penalty",
                    "accepted": True,
                    "racer_id": 1,
                    "penalty_seconds": 20,
                }
            ),
        )
        self.recorder._handle(
            recorder_module.EVENTS_CHANNEL,
            json.dumps(
                {
                    "type": "race_control",
                    "command": "disqualify_racer",
                    "accepted": True,
                    "racer_id": 2,
                }
            ),
        )

        self.assertEqual(self.recorder.engine.race.state, RaceState.FINISHED)
        self.assertEqual(len(self.end_race_commands()), 1)

        snapshot = self.snapshots()[-1]
        self.assertEqual(snapshot["state"], "finished")
        self.assertEqual(snapshot["penalties"], {"1": 20})
        self.assertEqual(snapshot["disqualified"], [2])
        leaderboard = snapshot["leaderboard"]
        assert isinstance(leaderboard, list)
        self.assertEqual(leaderboard[0]["racer_id"], 1)
        self.assertEqual(leaderboard[-1]["racer_id"], 2)
        self.assertTrue(leaderboard[-1]["disqualified"])

    def test_duplicate_and_malformed_messages_do_not_change_snapshots_or_laps(self):
        self.send_start_command(total_laps=2)
        self.send_start_event()
        initial_snapshot_count = len(self.snapshots())

        self.recorder._handle(recorder_module.HARDWARE_OUT_CHANNEL, "not json")
        self.recorder._handle(
            recorder_module.HARDWARE_OUT_CHANNEL,
            json.dumps({"type": "lap", "racer_id": 1, "race_start_at": START_AT}),
        )
        self.assertEqual(len(self.snapshots()), initial_snapshot_count)

        self.send_lap(1, 0.5)
        snapshot_count_after_lap = len(self.snapshots())
        self.send_lap(1, 0.5)

        self.assertEqual(len(self.snapshots()), snapshot_count_after_lap)
        self.assertEqual(len(self.recorder.engine.race.laps), 1)


if __name__ == "__main__":
    unittest.main()
