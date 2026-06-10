import unittest

from redis_commands import (
    CommandEnvelopeError,
    build_command_envelope,
    parse_command_envelope,
)


class TestRedisCommandEnvelope(unittest.TestCase):
    def test_build_command_envelope_adds_standard_metadata(self) -> None:
        payload = build_command_envelope("end_race", source="franklin_tui")

        self.assertEqual(payload["type"], "command")
        self.assertEqual(payload["command"], "end_race")
        self.assertEqual(payload["source"], "franklin_tui")
        self.assertIsInstance(payload["command_id"], str)
        self.assertTrue(payload["command_id"])
        self.assertIsInstance(payload["timestamp"], str)
        self.assertTrue(payload["timestamp"])

    def test_build_command_envelope_preserves_custom_fields(self) -> None:
        payload = build_command_envelope(
            "start_race",
            source="franklin_gui",
            ready_at=100.0,
            set_at=101.0,
            go_at=102.0,
            start_at=102.0,
        )

        self.assertEqual(payload["ready_at"], 100.0)
        self.assertEqual(payload["set_at"], 101.0)
        self.assertEqual(payload["go_at"], 102.0)
        self.assertEqual(payload["start_at"], 102.0)

    def test_build_command_envelope_honors_provided_metadata(self) -> None:
        payload = build_command_envelope(
            "reset_race",
            source="referee_web_app",
            command_id="my-command-id",
            timestamp="2026-06-09T12:00:00Z",
        )

        self.assertEqual(payload["command_id"], "my-command-id")
        self.assertEqual(payload["timestamp"], "2026-06-09T12:00:00Z")

    def test_parse_command_envelope_accepts_valid_payload(self) -> None:
        payload = {
            "type": "command",
            "command": "add_penalty",
            "command_id": "abc",
            "source": "referee_web_app",
            "timestamp": "2026-06-09T12:00:00Z",
            "racer_id": 2,
            "penalty_seconds": 5,
        }

        parsed = parse_command_envelope(payload)

        self.assertEqual(parsed["type"], "command")
        self.assertEqual(parsed["command"], "add_penalty")
        self.assertEqual(parsed["command_id"], "abc")
        self.assertEqual(parsed["source"], "referee_web_app")
        self.assertEqual(parsed["timestamp"], "2026-06-09T12:00:00Z")
        self.assertEqual(parsed["racer_id"], 2)
        self.assertEqual(parsed["penalty_seconds"], 5)

    def test_parse_command_envelope_rejects_missing_or_invalid_required_fields(
        self,
    ) -> None:
        with self.assertRaises(CommandEnvelopeError):
            parse_command_envelope({"command": "start_race"})

        with self.assertRaises(CommandEnvelopeError):
            parse_command_envelope(
                {
                    "type": "command",
                    "command": "start_race",
                    "source": "franklin_tui",
                    "timestamp": "2026-06-09T12:00:00Z",
                }
            )

        with self.assertRaises(CommandEnvelopeError):
            parse_command_envelope(
                {
                    "type": "command",
                    "command": "start_race",
                    "command_id": "abc",
                    "source": "",
                    "timestamp": "2026-06-09T12:00:00Z",
                }
            )


if __name__ == "__main__":
    unittest.main()
