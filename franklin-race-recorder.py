#!/usr/bin/env python3
"""Franklin headless race recorder.

A GUI-less daemon that owns the authoritative race model and (eventually) all
SQLite writes, driven entirely by Redis. It lets the GUI/TUI restart without
affecting the recording, and prevents the double-recording that happens when two
display clients each maintain their own model.

Responsibilities:
- subscribe to ``hardware:out`` + ``franklin:events`` and drive a ``RaceEngine``
- cache ``start_race`` race-config from ``hardware:in`` and apply it when the
  authoritative ``hardware:out`` ``start_race`` event arrives
- publish full snapshots on ``franklin:race_state`` and keep
  ``franklin:race_state:latest`` for late joiners
- publish ``end_race`` on automatic finish (only when authoritative)
- hold a single-owner Redis lock so two recorders never run at once

Redis contract: see docs/redis-message-reference.md (canonical).

Rollout: now that the GUI/TUI are pure renderers (they publish commands and
render ``franklin:race_state`` snapshots, never writing the DB), this recorder
defaults to **write mode**: it owns the authoritative model and is the sole
SQLite writer. Pass ``--shadow`` to consume events and publish snapshots
without writing SQLite (useful for debugging or running a second observer).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import time
from typing import Any
from uuid import uuid4

import redis

from database import LapDatabase
from race.race import generate_fake_race, order_laps_by_occurrence
from race.race_engine import RaceEngine
from race.race_mode import RaceMode
from race.race_state import RaceEndMode, is_race_going_state
from redis_commands import build_command_envelope

HARDWARE_IN_CHANNEL = "hardware:in"
HARDWARE_OUT_CHANNEL = "hardware:out"
EVENTS_CHANNEL = "franklin:events"
RACE_STATE_CHANNEL = "franklin:race_state"
RACE_STATE_LATEST_KEY = "franklin:race_state:latest"

LOCK_KEY = "franklin:race_recorder:lock"
LOCK_TTL_SECONDS = 10
LOCK_REFRESH_SECONDS = 4.0
SNAPSHOT_TICK_SECONDS = 1.0

SOURCE = "franklin_race_recorder"
VERSION = "0.2.0"


class RaceRecorder:
    def __init__(
        self,
        *,
        redis_socket: str,
        db_path: str,
        persist: bool,
    ) -> None:
        self.persist = persist
        self.redis = redis.Redis(unix_socket_path=redis_socket, decode_responses=True)
        self.db = LapDatabase(db_path)
        self.engine = RaceEngine(self.db, auto_resume=True, persist=persist)

        self._snapshot_seq = 0
        # Identifies this recorder run so clients can tell a restart apart from
        # an out-of-order message when snapshot_seq resets (see race_snapshot).
        self._recorder_id = uuid4().hex
        # Cached race-config from start_race commands, keyed by command_id, plus
        # the most recent one as a fallback when the echo carries no command_id.
        self._pending_start_config: dict[str, dict[str, Any]] = {}
        self._last_start_config: dict[str, Any] | None = None
        # Fake-race playback: scheduled (due_epoch, lap_message) entries that the
        # main loop ingests on time. Owning fake races here keeps every client a
        # pure renderer.
        self._fake_schedule: list[tuple[float, dict[str, Any]]] = []
        self._pending_start_event: tuple[float, dict[str, Any]] | None = None
        self._pending_command_start_event: tuple[float, dict[str, Any]] | None = None

        self._lock_value = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex}"
        self._running = True

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def run(self) -> int:
        try:
            self.redis.ping()
        except Exception as exc:
            logging.error("Cannot reach Redis: %s", exc)
            return 1

        if not self._acquire_lock():
            logging.error("Another race recorder already holds %s; exiting.", LOCK_KEY)
            return 1

        # Everything after we own the lock runs inside try/finally so the lock is
        # always released on any exit path (normal stop, signal, or exception).
        pubsub = None
        try:
            pubsub = self.redis.pubsub()
            pubsub.subscribe(HARDWARE_OUT_CHANNEL, EVENTS_CHANNEL, HARDWARE_IN_CHANNEL)
            mode = "WRITE (authoritative)" if self.persist else "SHADOW (no DB writes)"
            logging.info("Race recorder started in %s mode", mode)

            self._publish_snapshot()  # initial state for late joiners

            # Request hardware status
            try:
                status_env = build_command_envelope("request_status", source=SOURCE)
                self.redis.publish(HARDWARE_IN_CHANNEL, json.dumps(status_env))
                logging.info("Published request_status command on startup")
            except Exception as exc:
                logging.error(
                    "Failed to publish request_status command on startup: %s", exc
                )

            last_lock_refresh = time.monotonic()
            last_tick = time.monotonic()
            while self._running:
                message = pubsub.get_message(timeout=0.1)
                if message and message.get("type") == "message":
                    self._handle(message.get("channel"), message.get("data"))

                if self._fake_schedule:
                    self._process_fake_schedule(time.time())
                if self._pending_start_event:
                    self._process_pending_start(time.time())
                if self._pending_command_start_event:
                    self._process_pending_command_start(time.time())

                now = time.monotonic()
                if now - last_lock_refresh >= LOCK_REFRESH_SECONDS:
                    if not self._refresh_lock():
                        logging.error("Lost recorder lock; exiting.")
                        break
                    last_lock_refresh = now
                if now - last_tick >= SNAPSHOT_TICK_SECONDS:
                    # Refresh the retained snapshot so late joiners see a current
                    # clock even though live clients advance elapsed locally.
                    if is_race_going_state(self.engine.race.state):
                        self._publish_snapshot()
                    last_tick = now
        finally:
            self._finish_running_race_on_shutdown()
            if pubsub is not None:
                try:
                    pubsub.close()
                except Exception:
                    pass
            self._release_lock()
            self.db.close()
            logging.info("Race recorder stopped")
        return 0

    def stop(self, *_args: Any) -> None:
        self._running = False

    def _finish_running_race_on_shutdown(self) -> None:
        if not is_race_going_state(self.engine.race.state):
            return
        result = self.engine.end_race()
        if result.changed:
            logging.info("Ended running race during recorder shutdown")
            self._publish_snapshot()

    # ------------------------------------------------------------------ #
    # Message handling
    # ------------------------------------------------------------------ #
    def _handle(self, channel: Any, data: Any) -> None:
        if not isinstance(data, str):
            return
        try:
            msg = json.loads(data)
        except json.JSONDecodeError:
            logging.debug("Ignoring non-JSON message on %s", channel)
            return
        if not isinstance(msg, dict):
            return

        if channel == HARDWARE_IN_CHANNEL:
            self._handle_command(msg)
            return

        msg_type = msg.get("type")
        if msg_type == "start_race":
            self._handle_start_event(msg)
        elif msg_type == "lap":
            self._apply_and_publish(self.engine.ingest(msg))
        elif msg_type == "race_control":
            self._apply_and_publish(self.engine.apply_race_control(msg))
        elif msg_type == "hardware_status":
            logging.info(
                "Hardware monitor status: version=%s, simulation_mode=%s, hardware_connected=%s",
                msg.get("version"),
                msg.get("simulation_mode"),
                msg.get("hardware_connected"),
            )
        # heartbeat/status/countdown_phase/debug/error/hardware_status are display-only here.

    def _handle_command(self, msg: dict[str, Any]) -> None:
        """Cache race-config carried on ``start_race`` commands."""
        if msg.get("command") != "start_race":
            return
        config = self._config_from_command(msg)
        self._last_start_config = config
        command_id = msg.get("command_id")
        if isinstance(command_id, str) and command_id:
            self._pending_start_config[command_id] = config

        # The hardware monitor normally echoes a start_race event on
        # hardware:out after the countdown. Training mode should still work if
        # that echo is unavailable/stale, so schedule a recorder-owned fallback
        # from the command itself. A real hardware echo with the same command_id
        # cancels this fallback in _start_race_from_event().
        start_at_raw = msg.get("start_at") or msg.get("go_at")
        if isinstance(start_at_raw, (int, float)):
            fallback_msg = {
                "type": "start_race",
                "at": float(start_at_raw),
                "command_id": command_id,
                "source": msg.get("source"),
            }
            self._pending_command_start_event = (float(start_at_raw), fallback_msg)

    def _handle_start_event(self, msg: dict[str, Any]) -> None:
        at_raw = msg.get("at")
        if not isinstance(at_raw, (int, float)):
            logging.warning("start_race event missing 'at'; ignoring")
            return

        start_at = float(at_raw)
        if start_at > time.time():
            self._pending_start_event = (start_at, msg)
            logging.info("Queued start_race for %.3f", start_at)
            return

        self._start_race_from_event(msg, start_at)

    def _process_pending_start(self, now_epoch: float) -> None:
        if self._pending_start_event is None:
            return
        start_at, msg = self._pending_start_event
        if start_at > now_epoch:
            return
        self._pending_start_event = None
        self._start_race_from_event(msg, start_at)

    def _process_pending_command_start(self, now_epoch: float) -> None:
        if self._pending_command_start_event is None:
            return
        start_at, msg = self._pending_command_start_event
        if start_at > now_epoch:
            return
        self._pending_command_start_event = None
        if is_race_going_state(self.engine.race.state):
            return
        logging.info("Starting race from command fallback for %.3f", start_at)
        self._start_race_from_event(msg, start_at)

    def _start_race_from_event(self, msg: dict[str, Any], start_at: float) -> None:
        self._pending_start_event = None
        self._pending_command_start_event = None

        command_id = msg.get("command_id")
        config: dict[str, Any] | None = None
        if isinstance(command_id, str):
            config = self._pending_start_config.pop(command_id, None)
        if config is None:
            config = self._last_start_config or {}
        self._last_start_config = None

        race_mode = config.get("race_mode") or RaceMode.REAL
        total_laps = config.get("total_laps")
        race_end_mode = config.get("race_end_mode") or RaceEndMode.LAST_CAR
        if total_laps is None:
            total_laps = self.engine.total_laps
            logging.warning(
                "start_race had no total_laps config; falling back to %s", total_laps
            )

        result = self.engine.start(
            start_at=start_at,
            race_mode=race_mode,
            total_laps=int(total_laps),
            race_end_mode=race_end_mode,
        )

        # Fake races have no hardware; synthesize their laps here so clients stay
        # pure renderers.
        if race_mode == RaceMode.FAKE:
            self._build_fake_schedule(start_at)
        else:
            self._fake_schedule.clear()

        self._apply_and_publish(result)

    def _build_fake_schedule(self, start_at: float) -> None:
        fake_race = generate_fake_race()
        schedule: list[tuple[float, dict[str, Any]]] = []
        for cumulative_ts, lap in order_laps_by_occurrence(fake_race.laps):
            lap_at = start_at + cumulative_ts
            schedule.append(
                (
                    lap_at,
                    {
                        "type": "lap",
                        "racer_id": lap.racer_id,
                        "sensor_id": 1,
                        "race_start_at": start_at,
                        "lap_at": lap_at,
                        "recorded_at": lap_at,
                        "simulated": True,
                    },
                )
            )
        schedule.sort(key=lambda entry: entry[0])
        self._fake_schedule = schedule
        logging.info("Scheduled %s fake laps", len(schedule))

    def _process_fake_schedule(self, now_epoch: float) -> None:
        if not is_race_going_state(self.engine.race.state):
            self._fake_schedule.clear()
            return
        changed = False
        while self._fake_schedule and self._fake_schedule[0][0] <= now_epoch:
            _, lap_msg = self._fake_schedule.pop(0)
            result = self.engine.ingest(lap_msg)
            if getattr(result, "finished_now", False) and self.persist:
                self._publish_end_race()
            if getattr(result, "changed", False):
                changed = True
        if changed:
            self._publish_snapshot()

    def _config_from_command(self, cmd: dict[str, Any]) -> dict[str, Any]:
        config: dict[str, Any] = {}
        mode_raw = cmd.get("race_mode")
        if isinstance(mode_raw, str):
            try:
                config["race_mode"] = RaceMode(mode_raw)
            except ValueError:
                logging.warning("Unknown race_mode in start command: %r", mode_raw)
        total_raw = cmd.get("total_laps")
        if isinstance(total_raw, int):
            config["total_laps"] = total_raw
        end_raw = cmd.get("race_end_mode")
        if isinstance(end_raw, str):
            try:
                config["race_end_mode"] = RaceEndMode(end_raw)
            except ValueError:
                logging.warning("Unknown race_end_mode in start command: %r", end_raw)
        return config

    def _apply_and_publish(self, result: Any) -> None:
        if getattr(result, "finished_now", False) and self.persist:
            self._publish_end_race()
        if getattr(result, "changed", False):
            self._publish_snapshot()

    # ------------------------------------------------------------------ #
    # Publishing
    # ------------------------------------------------------------------ #
    def _publish_snapshot(self) -> None:
        self._snapshot_seq += 1
        snapshot = self.engine.build_snapshot(snapshot_seq=self._snapshot_seq)
        snapshot["recorder_id"] = self._recorder_id
        payload = json.dumps(snapshot)
        try:
            self.redis.set(RACE_STATE_LATEST_KEY, payload)
            self.redis.publish(RACE_STATE_CHANNEL, payload)
        except Exception as exc:
            logging.error("Failed to publish snapshot: %s", exc)

    def _publish_end_race(self) -> None:
        try:
            envelope = build_command_envelope("end_race", source=SOURCE)
            self.redis.publish(HARDWARE_IN_CHANNEL, json.dumps(envelope))
            logging.info("Published auto-finish end_race command")
        except Exception as exc:
            logging.error("Failed to publish end_race: %s", exc)

    # ------------------------------------------------------------------ #
    # Single-owner lock
    # ------------------------------------------------------------------ #
    def _acquire_lock(self) -> bool:
        try:
            return bool(
                self.redis.set(LOCK_KEY, self._lock_value, nx=True, ex=LOCK_TTL_SECONDS)
            )
        except Exception as exc:
            logging.error("Failed to acquire lock: %s", exc)
            return False

    def _refresh_lock(self) -> bool:
        try:
            if self.redis.get(LOCK_KEY) != self._lock_value:
                return False
            self.redis.expire(LOCK_KEY, LOCK_TTL_SECONDS)
            return True
        except Exception as exc:
            logging.error("Failed to refresh lock: %s", exc)
            return False

    # Atomic compare-and-delete: only remove the lock if we still own it, so a
    # late release can never clobber a lock a different recorder has acquired.
    _RELEASE_LUA = (
        "if redis.call('get', KEYS[1]) == ARGV[1] "
        "then return redis.call('del', KEYS[1]) else return 0 end"
    )

    def _release_lock(self) -> None:
        try:
            self.redis.eval(self._RELEASE_LUA, 1, LOCK_KEY, self._lock_value)
            logging.info("Released recorder lock %s", LOCK_KEY)
        except Exception as exc:  # pragma: no cover - defensive
            logging.debug("Failed to release lock: %s", exc)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Franklin headless race recorder")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
        help="Show program's version number and exit",
    )
    parser.add_argument(
        "--redis-socket",
        default="./redis.sock",
        help="Path to the Redis unix socket (default: ./redis.sock)",
    )
    parser.add_argument(
        "--db",
        default="franklin.db",
        help="Path to the SQLite database (default: franklin.db)",
    )
    write_group = parser.add_mutually_exclusive_group()
    write_group.add_argument(
        "--write",
        dest="persist",
        action="store_true",
        help="Authoritative mode (default): own the model and write SQLite.",
    )
    write_group.add_argument(
        "--shadow",
        dest="persist",
        action="store_false",
        help="Shadow mode: consume + publish snapshots but do NOT write SQLite "
        "(for debugging or a second observer alongside the writer).",
    )
    parser.set_defaults(persist=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        filename="race_recorder.log",
        filemode="a",
        format="%(asctime)s %(levelname)s:%(message)s",
        level=logging.INFO,
        force=True,
    )
    # Also log to stderr so the daemon is observable in a terminal/pane.
    logging.getLogger().addHandler(logging.StreamHandler())

    args = parse_args(argv)

    logging.info("Starting franklin-race-recorder v%s", VERSION)
    recorder = RaceRecorder(
        redis_socket=args.redis_socket,
        db_path=args.db,
        persist=args.persist,
    )
    signal.signal(signal.SIGINT, recorder.stop)
    signal.signal(signal.SIGTERM, recorder.stop)
    return recorder.run()


if __name__ == "__main__":
    raise SystemExit(main())
