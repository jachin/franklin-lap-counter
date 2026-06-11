"""Client-side parsing of ``franklin:race_state`` snapshots.

The headless recorder publishes authoritative race snapshots (see
``docs/redis-message-reference.md``). Read-only views (GUI, TUI) parse them with
:class:`RaceSnapshot` instead of maintaining their own :class:`~race.race.Race`
model. This module is pure data + a local clock helper so it is trivially
unit-testable and shared by every view.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SnapshotLeaderRow:
    position: int | None
    racer_id: int
    lap_count: int
    best_lap_time: float | None
    last_lap_time: float | None
    raw_total_time: float
    penalty_seconds: int
    adjusted_total_time: float
    disqualified: bool

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SnapshotLeaderRow":
        return cls(
            position=data.get("position"),
            racer_id=int(data["racer_id"]),
            lap_count=int(data.get("lap_count", 0)),
            best_lap_time=_opt_float(data.get("best_lap_time")),
            last_lap_time=_opt_float(data.get("last_lap_time")),
            raw_total_time=float(data.get("raw_total_time", 0.0)),
            penalty_seconds=int(data.get("penalty_seconds", 0)),
            adjusted_total_time=float(data.get("adjusted_total_time", 0.0)),
            disqualified=bool(data.get("disqualified", False)),
        )


@dataclass
class SnapshotLap:
    racer_id: int
    lap_number: int
    lap_time: float
    race_time: float
    race_start_at: float
    lap_at: float
    recorded_at: float

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SnapshotLap":
        return cls(
            racer_id=int(data["racer_id"]),
            lap_number=int(data.get("lap_number", 0)),
            lap_time=float(data.get("lap_time", 0.0)),
            race_time=float(data.get("race_time", 0.0)),
            race_start_at=float(data.get("race_start_at", 0.0)),
            lap_at=float(data.get("lap_at", 0.0)),
            recorded_at=float(data.get("recorded_at", 0.0)),
        )


# Race states that are still "going" (clock should advance locally).
GOING_STATES = frozenset({"running", "winner_declared"})


@dataclass
class RaceSnapshot:
    schema_version: int
    snapshot_seq: int
    snapshot_at: float
    state: str
    race_id: int | None
    start_at: float | None
    end_at: float | None
    elapsed_seconds: float
    race_mode: str
    total_laps: int
    effective_total_laps: int
    race_end_mode: str
    leaderboard: list[SnapshotLeaderRow]
    laps_remaining_leader: int
    laps_remaining_last: int
    penalties: dict[int, int]
    disqualified: list[int]
    laps: list[SnapshotLap]
    # Identifies the recorder run that produced this snapshot. Lets clients tell
    # a recorder restart (snapshot_seq resets to 1) apart from an out-of-order
    # message from the same run.
    recorder_id: str = ""
    # Local monotonic time at which this snapshot was received, used to advance
    # the displayed clock between snapshots without trusting cross-host wall time.
    received_monotonic: float = field(default_factory=time.monotonic)

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], *, received_monotonic: float | None = None
    ) -> "RaceSnapshot":
        laps_remaining = data.get("laps_remaining") or {}
        return cls(
            schema_version=int(data.get("schema_version", 0)),
            snapshot_seq=int(data.get("snapshot_seq", 0)),
            snapshot_at=float(data.get("snapshot_at", 0.0)),
            state=str(data.get("state", "not_started")),
            race_id=_opt_int(data.get("race_id")),
            start_at=_opt_float(data.get("start_at")),
            end_at=_opt_float(data.get("end_at")),
            elapsed_seconds=float(data.get("elapsed_seconds", 0.0)),
            race_mode=str(data.get("race_mode", "")),
            total_laps=int(data.get("total_laps", 0)),
            effective_total_laps=int(data.get("effective_total_laps", 0)),
            race_end_mode=str(data.get("race_end_mode", "")),
            leaderboard=[
                SnapshotLeaderRow.from_dict(row)
                for row in data.get("leaderboard", [])
            ],
            laps_remaining_leader=int(laps_remaining.get("leader", 0)),
            laps_remaining_last=int(laps_remaining.get("last_place", 0)),
            penalties={
                int(k): int(v) for k, v in (data.get("penalties") or {}).items()
            },
            disqualified=[int(x) for x in data.get("disqualified", [])],
            laps=[SnapshotLap.from_dict(lap) for lap in data.get("laps", [])],
            recorder_id=str(data.get("recorder_id", "")),
            received_monotonic=(
                received_monotonic
                if received_monotonic is not None
                else time.monotonic()
            ),
        )

    @property
    def is_going(self) -> bool:
        return self.state in GOING_STATES

    def current_elapsed(self, now_monotonic: float | None = None) -> float:
        """Displayed elapsed time, advanced locally while the race is going.

        Frozen at ``elapsed_seconds`` for finished/paused/idle states.
        """
        if self.state not in GOING_STATES:
            return self.elapsed_seconds
        now = time.monotonic() if now_monotonic is None else now_monotonic
        return self.elapsed_seconds + max(0.0, now - self.received_monotonic)

    def supersedes(self, other: "RaceSnapshot | None") -> bool:
        """True if this snapshot is newer than ``other`` (drop stale/out-of-order).

        Within one recorder run, the monotonic ``snapshot_seq`` decides. Across a
        recorder restart (different ``recorder_id``, where ``snapshot_seq`` resets
        to 1) we fall back to the newer ``snapshot_at`` so a fresh run is not
        dropped as stale.
        """
        if other is None:
            return True
        if self.recorder_id and other.recorder_id and self.recorder_id != other.recorder_id:
            return self.snapshot_at >= other.snapshot_at
        return self.snapshot_seq > other.snapshot_seq


def idle_snapshot() -> RaceSnapshot:
    """Neutral state used before the first snapshot arrives."""
    return RaceSnapshot.from_dict(
        {
            "schema_version": 1,
            "snapshot_seq": 0,
            "snapshot_at": 0.0,
            "state": "not_started",
            "race_id": None,
            "start_at": None,
            "end_at": None,
            "elapsed_seconds": 0.0,
            "race_mode": "",
            "total_laps": 0,
            "effective_total_laps": 0,
            "race_end_mode": "",
            "leaderboard": [],
            "laps_remaining": {"leader": 0, "last_place": 0},
            "penalties": {},
            "disqualified": [],
            "laps": [],
        }
    )


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _opt_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
