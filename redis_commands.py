"""Shared helpers for Redis `hardware:in` command envelopes.

Authoritative contract: docs/redis-message-reference.md
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Mapping
from uuid import uuid4

CommandEnvelope = dict[str, Any]


class CommandEnvelopeError(ValueError):
    """Raised when a command payload does not match expected envelope shape."""


def _iso_utc_now() -> str:
    return datetime.now(UTC).isoformat()


def build_command_envelope(
    command: str,
    *,
    source: str,
    command_id: str | None = None,
    timestamp: str | None = None,
    **fields: Any,
) -> CommandEnvelope:
    """Build a canonical `hardware:in` command envelope.

    All producers should call this helper so metadata is consistent.
    """
    envelope: CommandEnvelope = {
        "type": "command",
        "command": command,
        "command_id": command_id or str(uuid4()),
        "source": source,
        "timestamp": timestamp or _iso_utc_now(),
    }
    envelope.update(fields)
    return envelope


def parse_command_envelope(payload: Mapping[str, Any]) -> CommandEnvelope:
    """Validate and normalize one command envelope.

    Returns a plain `dict` with canonical keys when valid.
    """
    msg_type = payload.get("type")
    if msg_type != "command":
        raise CommandEnvelopeError("Command payload must include type='command'")

    command = payload.get("command")
    if not isinstance(command, str) or not command:
        raise CommandEnvelopeError("Command payload must include non-empty 'command'")

    command_id = payload.get("command_id")
    source = payload.get("source")
    timestamp = payload.get("timestamp")

    if not isinstance(command_id, str) or not command_id:
        raise CommandEnvelopeError(
            "Command payload must include non-empty 'command_id'"
        )
    if not isinstance(source, str) or not source:
        raise CommandEnvelopeError("Command payload must include non-empty 'source'")
    if not isinstance(timestamp, str) or not timestamp:
        raise CommandEnvelopeError("Command payload must include non-empty 'timestamp'")

    normalized: CommandEnvelope = dict(payload)
    normalized["type"] = "command"
    normalized["command"] = command
    normalized["command_id"] = command_id
    normalized["source"] = source
    normalized["timestamp"] = timestamp
    return normalized
