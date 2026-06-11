"""Authoritative race model + persistence engine.

`RaceEngine` owns the in-memory :class:`~race.race.Race`, applies the same lap /
race-control / reset logic that the GUI and TUI currently duplicate, and is the
single writer to the SQLite database. It performs **no Redis I/O**: callers feed
it parsed message dicts (or call the explicit methods) and read back an
:class:`EngineResult` plus :meth:`RaceEngine.build_snapshot` output. This keeps
the model deterministic and unit-testable; the headless recorder daemon wraps it
with the actual pub/sub transport.

See ``docs/redis-message-reference.md`` for the ``franklin:race_state`` snapshot
contract this module produces.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from race.lap import EpochSeconds, Lap, LapTime
from race.race import Race, make_lap_from_sensor_event_and_race
from race.race_end_logic import resolve_post_lap_state
from race.race_mode import RaceMode
from race.race_state import RaceEndMode, RaceState, is_race_going_state

if TYPE_CHECKING:
    from database import LapDatabase

# Training (practice) mode never auto-finishes; drivers keep lapping until the
# session is ended manually. This effectively-unlimited target prevents the race
# engine from declaring a winner or capping laps. Mirrors franklin-gui.py.
TRAINING_LAP_TARGET = 1_000_000

SNAPSHOT_SCHEMA_VERSION = 1


@dataclass
class EngineResult:
    """Outcome of an ingest/control call.

    ``changed`` tells the caller whether to publish a fresh snapshot.
    ``finished_now`` tells it to publish an ``end_race`` command.
    ``note`` is a short human/debug string (and the ignore reason when the
    action was not applied).
    """

    changed: bool = False
    finished_now: bool = False
    note: str = ""
    lap_number: int | None = None


@dataclass
class _RaceConfig:
    mode: RaceMode = RaceMode.REAL
    total_laps: int = 10
    end_mode: RaceEndMode = RaceEndMode.LAST_CAR


class RaceEngine:
    """Owns the race model + SQLite persistence; produces snapshots."""

    def __init__(
        self, db: "LapDatabase", *, auto_resume: bool = True, persist: bool = True
    ) -> None:
        self.db = db
        # When False the engine never writes to SQLite (shadow mode): it still
        # reads for resume and computes/serves snapshots, but leaves all writes
        # to whoever currently owns them. Lets the recorder run alongside the
        # unmodified GUI/TUI without double-writing during rollout.
        self.persist = persist
        self.race: Race = Race()
        self.previous_race: Race | None = None
        self.current_race_id: int | None = None

        # User-facing settings (effective values live on ``self.race``).
        self.race_mode: RaceMode = RaceMode.REAL
        self.total_laps: int = 10
        self.race_end_mode: RaceEndMode = RaceEndMode.LAST_CAR

        # Referee adjustments (persisted by the command producer's audit rows).
        self.racer_penalties_seconds: dict[int, int] = {}
        self.disqualified_racers: set[int] = set()

        # Epoch clock anchors for snapshots (process-independent).
        self._start_at_epoch: float | None = None
        self._end_at_epoch: float | None = None

        # Dedupe guard so a redelivered lap event is not double-counted.
        self._seen_laps: set[tuple[int, float]] = set()

        if auto_resume:
            self.resume_from_db()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(
        self,
        *,
        start_at: float,
        race_mode: RaceMode,
        total_laps: int,
        race_end_mode: RaceEndMode,
    ) -> EngineResult:
        """Begin a new race and create its DB row.

        Training mode is mapped to an effectively unlimited target with manual
        end mode, matching the existing GUI/TUI behavior.
        """
        self.race_mode = race_mode
        self.total_laps = total_laps
        self.race_end_mode = race_end_mode

        race = Race(previous_race=self.previous_race)
        if race_mode == RaceMode.TRAINING:
            race.total_laps = TRAINING_LAP_TARGET
            race.race_end_mode = RaceEndMode.MANUAL
        else:
            race.total_laps = total_laps
            race.race_end_mode = race_end_mode
        race.start(start_time=time.monotonic())

        self.race = race
        self._start_at_epoch = float(start_at)
        self._end_at_epoch = None
        self.racer_penalties_seconds.clear()
        self.disqualified_racers.clear()
        self._seen_laps.clear()
        if self.persist:
            self.current_race_id = self.db.create_race(
                notes=self._encode_notes(), start_at=float(start_at)
            )
        else:
            # Shadow mode: adopt the race id whoever owns writes just created,
            # so snapshots still carry a meaningful race_id.
            row = self.db.get_in_progress_race()
            self.current_race_id = int(row["id"]) if row else None
        logging.info(
            "RaceEngine started race %s (mode=%s, total_laps=%s, end_mode=%s)",
            self.current_race_id,
            race_mode,
            race.total_laps,
            race.race_end_mode.value,
        )
        return EngineResult(changed=True, note="started")

    def end_race(self, *, at: float | None = None) -> EngineResult:
        """Manually finish the current race (e.g. operator pressed End)."""
        if not is_race_going_state(self.race.state):
            return EngineResult(changed=False, note="not_running")
        self.race.state = RaceState.FINISHED
        self._finalize(end_at=at)
        return EngineResult(changed=True, finished_now=False, note="ended")

    def reset(self) -> EngineResult:
        """Clear the current race back to a fresh, not-started state."""
        self.racer_penalties_seconds.clear()
        self.disqualified_racers.clear()
        self._seen_laps.clear()

        if self.current_race_id is not None:
            if self.persist:
                try:
                    self.db.end_race(self.current_race_id)
                except Exception as exc:  # pragma: no cover - defensive
                    logging.error("Failed to end race during reset: %s", exc)
            self.current_race_id = None

        race = Race(previous_race=self.previous_race)
        race.total_laps = self.total_laps
        race.race_end_mode = self.race_end_mode
        self.race = race
        self._start_at_epoch = None
        self._end_at_epoch = None
        return EngineResult(changed=True, note="reset")

    def _finalize(self, *, end_at: float | None = None) -> None:
        self.previous_race = self.race
        self._end_at_epoch = float(end_at) if end_at is not None else time.time()
        if self.current_race_id is not None and self.persist:
            try:
                self.db.end_race(self.current_race_id, end_at=self._end_at_epoch)
            except Exception as exc:  # pragma: no cover - defensive
                logging.error("Failed to mark race finished: %s", exc)
        # current_race_id is intentionally retained so views can keep showing
        # the just-finished race until an explicit reset/new race.

    # ------------------------------------------------------------------ #
    # Event ingestion
    # ------------------------------------------------------------------ #
    def ingest(self, msg: dict[str, Any]) -> EngineResult:
        """Route a parsed ``hardware:out`` / ``franklin:events`` message.

        ``start_race`` is intentionally not handled here: it needs race config
        the caller resolves (see ``start``). Display-only events are ignored.
        """
        msg_type = msg.get("type")
        if msg_type == "lap":
            return self.record_lap(
                racer_id=msg.get("racer_id"),
                sensor_id=msg.get("sensor_id"),
                race_start_at=msg.get("race_start_at"),
                lap_at=msg.get("lap_at"),
                recorded_at=msg.get("recorded_at"),
                simulated=bool(msg.get("simulated", False)),
            )
        if msg_type == "race_control":
            return self.apply_race_control(msg)
        return EngineResult(changed=False, note=f"ignored:{msg_type}")

    def record_lap(
        self,
        *,
        racer_id: Any,
        race_start_at: Any,
        lap_at: Any,
        sensor_id: Any = None,
        recorded_at: Any = None,
        simulated: bool = False,
    ) -> EngineResult:
        if not is_race_going_state(self.race.state):
            return EngineResult(changed=False, note="race_not_running")
        if racer_id is None or not isinstance(lap_at, (int, float)):
            return EngineResult(changed=False, note="invalid_lap")
        if not isinstance(race_start_at, (int, float)):
            return EngineResult(changed=False, note="invalid_lap")

        racer_id_i = int(racer_id)
        if racer_id_i in self.disqualified_racers:
            return EngineResult(changed=False, note="disqualified")

        lap_at_f = float(lap_at)
        key = (racer_id_i, round(lap_at_f, 3))
        if key in self._seen_laps:
            return EngineResult(changed=False, note="duplicate")

        recorded_f = (
            float(recorded_at) if isinstance(recorded_at, (int, float)) else lap_at_f
        )
        race_start_f = float(race_start_at)
        lap = make_lap_from_sensor_event_and_race(
            racer_id_i,
            race_start_at=race_start_f,
            lap_at=lap_at_f,
            recorded_at=recorded_f,
            race=self.race,
        )

        previous_state = self.race.state
        if not self.race.add_lap(lap):
            return EngineResult(changed=False, note="already_finished")
        self._seen_laps.add(key)

        finished_now = (
            previous_state != RaceState.FINISHED
            and self.race.state == RaceState.FINISHED
        )

        sensor_id_i = int(sensor_id) if sensor_id is not None else racer_id_i
        if self.current_race_id is not None and self.persist:
            self.db.add_lap(
                race_id=self.current_race_id,
                racer_id=lap.racer_id,
                sensor_id=sensor_id_i,
                lap_number=lap.lap_number,
                lap_time=lap.lap_time if lap.lap_number > 0 else None,
                race_start_at=race_start_f,
                lap_at=lap_at_f,
                recorded_at=recorded_f,
            )

        if finished_now:
            self._finalize(end_at=recorded_f)

        return EngineResult(
            changed=True, finished_now=finished_now, lap_number=lap.lap_number
        )

    def apply_race_control(self, msg: dict[str, Any]) -> EngineResult:
        command = str(msg.get("command", ""))
        accepted = bool(msg.get("accepted", True))
        racer_id_raw = msg.get("racer_id")
        racer_id = int(racer_id_raw) if racer_id_raw is not None else None

        if not accepted:
            return EngineResult(changed=False, note=f"rejected:{command}")

        if command == "reset_race":
            return self.reset()
        if command == "end_race":
            return self.end_race()
        if command == "add_penalty" and racer_id is not None:
            return self.add_penalty(racer_id, int(msg.get("penalty_seconds", 0) or 0))
        if command == "disqualify_racer" and racer_id is not None:
            return self.disqualify(racer_id)
        if command == "remove_lap" and racer_id is not None:
            lap_no_raw = msg.get("lap_number")
            lap_no = int(lap_no_raw) if lap_no_raw is not None else None
            return self.remove_lap(racer_id, lap_no)
        return EngineResult(changed=False, note=f"unhandled:{command}")

    # ------------------------------------------------------------------ #
    # Referee adjustments
    # ------------------------------------------------------------------ #
    def add_penalty(self, racer_id: int, penalty_seconds: int) -> EngineResult:
        if penalty_seconds <= 0:
            return EngineResult(changed=False, note="invalid_penalty")
        racer_id = int(racer_id)
        self.racer_penalties_seconds[racer_id] = (
            self.racer_penalties_seconds.get(racer_id, 0) + penalty_seconds
        )
        return EngineResult(changed=True, note="penalty")

    def disqualify(self, racer_id: int) -> EngineResult:
        racer_id = int(racer_id)
        if racer_id in self.disqualified_racers:
            return EngineResult(changed=False, note="already_dq")
        self.disqualified_racers.add(racer_id)
        # Exclude from auto-finish checks so a DQ'd racer who never reaches the
        # lap target cannot stall LAST_CAR completion.
        self.race.active_contestants.discard(racer_id)
        finished_now = self._reresolve_finish()
        return EngineResult(changed=True, finished_now=finished_now, note="dq")

    def remove_lap(
        self, racer_id: int, lap_number: int | None = None
    ) -> EngineResult:
        racer_id = int(racer_id)
        target_index: int | None = None
        for idx in range(len(self.race.laps) - 1, -1, -1):
            lap = self.race.laps[idx]
            if lap.racer_id != racer_id or lap.lap_number <= 0:
                continue
            if lap_number is not None and lap.lap_number != lap_number:
                continue
            target_index = idx
            break

        if target_index is None:
            return EngineResult(changed=False, note="lap_not_found")

        removed = self.race.laps.pop(target_index)
        self._seen_laps.discard((racer_id, round(float(removed.lap_at), 3)))

        if self.current_race_id is not None and self.persist:
            try:
                self.db.remove_lap(self.current_race_id, racer_id, lap_number)
            except Exception as exc:  # pragma: no cover - defensive
                logging.error("Failed to remove lap in DB: %s", exc)

        return EngineResult(changed=True, note=f"removed lap {removed.lap_number}")

    def _reresolve_finish(self) -> bool:
        if self.race.state not in (RaceState.RUNNING, RaceState.WINNER_DECLARED):
            return False
        previous_state = self.race.state
        self.race.state = resolve_post_lap_state(
            current_state=self.race.state,
            race_end_mode=self.race.race_end_mode,
            total_laps=self.race.total_laps,
            leaderboard=self.race.leaderboard(),
            active_contestants=self.race.active_contestants,
        )
        finished_now = (
            previous_state != RaceState.FINISHED
            and self.race.state == RaceState.FINISHED
        )
        if finished_now:
            self._finalize()
        return finished_now

    # ------------------------------------------------------------------ #
    # Persistence resume
    # ------------------------------------------------------------------ #
    def resume_from_db(self) -> bool:
        """Rebuild the live race from a persisted in-progress race, if any."""
        race_row = self.db.get_in_progress_race()
        if not race_row:
            return False

        race_id = int(race_row["id"])
        self.current_race_id = race_id
        config = self._decode_notes(str(race_row.get("notes") or ""))
        self.race_mode = config.mode
        self.total_laps = config.total_laps
        self.race_end_mode = config.end_mode

        race = Race(previous_race=self.previous_race)
        if config.mode == RaceMode.TRAINING:
            race.total_laps = TRAINING_LAP_TARGET
            race.race_end_mode = RaceEndMode.MANUAL
        else:
            race.total_laps = config.total_laps
            race.race_end_mode = config.end_mode

        race_start_epoch = race_row.get("start_at")
        restored_laps: list[Lap] = []
        for lap_row in self.db.get_race_laps(race_id):
            lap = self._lap_from_db_row(lap_row, race_start_epoch)
            if lap is not None:
                restored_laps.append(lap)

        race.laps = restored_laps
        race.active_contestants = {
            lap.racer_id for lap in restored_laps if lap.lap_number > 0
        }
        race.state = RaceState.RUNNING

        if isinstance(race_start_epoch, (int, float)) and race_start_epoch > 0:
            self._start_at_epoch = float(race_start_epoch)
            elapsed = max(0.0, time.time() - self._start_at_epoch)
            race.start_time = time.monotonic() - elapsed
            race.elapsed_time = elapsed
        else:
            self._start_at_epoch = None
            race.start_time = time.monotonic()
            race.elapsed_time = 0.0

        self.race = race
        self._end_at_epoch = None
        self._seen_laps = {
            (lap.racer_id, round(float(lap.lap_at), 3)) for lap in restored_laps
        }
        self._restore_referee_adjustments(race_id)

        # A DQ'd racer is excluded from finish checks (mirrors disqualify()).
        for racer_id in self.disqualified_racers:
            self.race.active_contestants.discard(racer_id)

        logging.info(
            "RaceEngine resumed race %s: %s laps, %s racers, mode=%s",
            race_id,
            len(restored_laps),
            len(race.active_contestants),
            config.mode,
        )
        return True

    def _restore_referee_adjustments(self, race_id: int) -> None:
        for action in self.db.get_race_control_actions(race_id=race_id):
            if not action.get("accepted"):
                continue
            command = action.get("command")
            racer_id_raw = action.get("racer_id")
            if racer_id_raw is None:
                continue
            racer_id = int(racer_id_raw)
            if command == "add_penalty":
                penalty_seconds = int(action.get("penalty_seconds") or 0)
                if penalty_seconds > 0:
                    self.racer_penalties_seconds[racer_id] = (
                        self.racer_penalties_seconds.get(racer_id, 0) + penalty_seconds
                    )
            elif command == "disqualify_racer":
                self.disqualified_racers.add(racer_id)

    def _lap_from_db_row(
        self, lap_row: dict[str, Any], race_start_epoch: Any
    ) -> Lap | None:
        race_start = lap_row.get("race_start_at")
        if not isinstance(race_start, (int, float)) or race_start <= 0:
            race_start = race_start_epoch
        lap_at = lap_row.get("lap_at")
        recorded_at = lap_row.get("recorded_at")
        lap_number = int(lap_row.get("lap_number", 0))
        lap_time = lap_row.get("lap_time")

        if not isinstance(race_start, (int, float)) or race_start <= 0:
            return None
        if not isinstance(lap_at, (int, float)) or lap_at <= 0:
            return None
        if not isinstance(recorded_at, (int, float)) or recorded_at <= 0:
            recorded_at = lap_at
        if not isinstance(lap_time, (int, float)):
            lap_time = float(lap_at) - float(race_start)

        try:
            return Lap(
                racer_id=int(lap_row["racer_id"]),
                lap_number=lap_number,
                race_start_at=EpochSeconds(float(race_start)),
                lap_at=EpochSeconds(float(lap_at)),
                recorded_at=EpochSeconds(float(recorded_at)),
                lap_time=LapTime(float(lap_time)),
            )
        except (ValueError, KeyError, TypeError) as exc:
            logging.warning("Skipping unrestorable lap row %s: %s", lap_row, exc)
            return None

    # ------------------------------------------------------------------ #
    # Race-config notes encode/decode (DB ``races.notes``)
    # ------------------------------------------------------------------ #
    def _encode_notes(self) -> str:
        return json.dumps(
            {
                "mode": self.race_mode.value,
                "total_laps": self.total_laps,
                "end_mode": self.race_end_mode.value,
            }
        )

    def _decode_notes(self, notes: str) -> _RaceConfig:
        config = _RaceConfig()
        notes = notes or ""

        try:
            data = json.loads(notes)
        except (json.JSONDecodeError, TypeError):
            data = None

        if isinstance(data, dict):
            mode_raw = data.get("mode")
            if mode_raw is not None:
                try:
                    config.mode = RaceMode(mode_raw)
                except ValueError:
                    pass
            total_laps_raw = data.get("total_laps")
            if isinstance(total_laps_raw, int):
                config.total_laps = total_laps_raw
            end_mode_raw = data.get("end_mode")
            if end_mode_raw is not None:
                try:
                    config.end_mode = RaceEndMode(end_mode_raw)
                except ValueError:
                    pass
            return config

        # Legacy free-text notes: "Mode: <value>, Total Laps: N[, End Mode: x]".
        for mode in RaceMode:
            if mode.value in notes:
                config.mode = mode
                break
        laps_match = re.search(r"Total Laps:\s*(\d+)", notes)
        if laps_match:
            config.total_laps = int(laps_match.group(1))
        end_match = re.search(r"End Mode:\s*(\w+)", notes)
        if end_match:
            try:
                config.end_mode = RaceEndMode(end_match.group(1))
            except ValueError:
                pass
        return config

    # ------------------------------------------------------------------ #
    # Snapshot
    # ------------------------------------------------------------------ #
    def build_snapshot(self, *, snapshot_seq: int) -> dict[str, Any]:
        """Produce a ``franklin:race_state`` snapshot dict (see canonical doc)."""
        now = time.time()
        leader_remaining, last_remaining = self.race.laps_remaining()
        return {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "snapshot_seq": snapshot_seq,
            "snapshot_at": now,
            "state": self.race.state.name.lower(),
            "race_id": self.current_race_id,
            "start_at": self._start_at_epoch,
            "end_at": self._end_at_epoch,
            "elapsed_seconds": self._elapsed_seconds(now),
            "race_mode": self.race_mode.value,
            "total_laps": self.total_laps,
            "effective_total_laps": self.race.total_laps,
            "race_end_mode": self.race.race_end_mode.value,
            "leaderboard": self._snapshot_leaderboard(),
            "laps_remaining": {
                "leader": leader_remaining,
                "last_place": last_remaining,
            },
            "penalties": {
                str(rid): sec for rid, sec in self.racer_penalties_seconds.items()
            },
            "disqualified": sorted(self.disqualified_racers),
            "laps": self._snapshot_laps(),
        }

    def _elapsed_seconds(self, now: float) -> float:
        if self._start_at_epoch is None:
            return 0.0
        if self.race.state in (RaceState.RUNNING, RaceState.WINNER_DECLARED):
            return max(0.0, now - self._start_at_epoch)
        if self.race.state == RaceState.FINISHED and self._end_at_epoch is not None:
            return max(0.0, self._end_at_epoch - self._start_at_epoch)
        return 0.0

    def adjusted_leaderboard(
        self,
    ) -> list[tuple[int | None, int, int, float, float, float, int, float, bool]]:
        """Penalty/DQ-adjusted, display-ordered leaderboard rows.

        Each tuple: (position, racer_id, lap_count, best, last, raw_total,
        penalty_seconds, adjusted_total, disqualified). ``position`` is ``None``
        for DQ rows. ``best``/``last`` keep ``inf`` for missing values; the
        snapshot serializer normalizes those to ``None``.
        """
        base = self.race.leaderboard()
        active: list[tuple[int, int, float, float, float, int, float]] = []
        dq: list[tuple[int, int, float, float, float, int, float]] = []

        for _pos, racer_id, lap_count, best, last, total in base:
            penalty = int(self.racer_penalties_seconds.get(racer_id, 0))
            adjusted = total + float(penalty)
            row = (racer_id, lap_count, best, last, total, penalty, adjusted)
            if racer_id in self.disqualified_racers:
                dq.append(row)
            else:
                active.append(row)

        active.sort(
            key=lambda r: (
                -r[1],  # lap_count desc
                r[6],  # adjusted total asc
                r[2],  # best lap asc
                r[0],  # stable tie-breaker by racer_id
            )
        )
        dq.sort(key=lambda r: r[0])

        rows: list[
            tuple[int | None, int, int, float, float, float, int, float, bool]
        ] = []
        for position, row in enumerate(active, start=1):
            racer_id, lap_count, best, last, total, penalty, adjusted = row
            rows.append(
                (position, racer_id, lap_count, best, last, total, penalty, adjusted, False)
            )
        for row in dq:
            racer_id, lap_count, best, last, total, penalty, adjusted = row
            rows.append(
                (None, racer_id, lap_count, best, last, total, penalty, adjusted, True)
            )
        return rows

    def _snapshot_leaderboard(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for (
            position,
            racer_id,
            lap_count,
            best,
            last,
            raw_total,
            penalty,
            adjusted,
            disqualified,
        ) in self.adjusted_leaderboard():
            result.append(
                {
                    "position": position,
                    "racer_id": racer_id,
                    "lap_count": lap_count,
                    "best_lap_time": _finite(best),
                    "last_lap_time": _finite(last),
                    "raw_total_time": raw_total,
                    "penalty_seconds": penalty,
                    "adjusted_total_time": adjusted,
                    "disqualified": disqualified,
                }
            )
        return result

    def _snapshot_laps(self) -> list[dict[str, Any]]:
        return [
            {
                "racer_id": lap.racer_id,
                "lap_number": lap.lap_number,
                "lap_time": float(lap.lap_time),
                "race_time": lap.seconds_from_race_start,
                "race_start_at": float(lap.race_start_at),
                "lap_at": float(lap.lap_at),
                "recorded_at": float(lap.recorded_at),
            }
            for lap in self.race.laps
        ]


def _finite(value: float) -> float | None:
    """Normalize ``inf``/``nan`` to ``None`` for JSON-safe snapshots."""
    if value == float("inf") or value != value:
        return None
    return value
