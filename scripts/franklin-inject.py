#!/usr/bin/env python3
"""CLI tool to inject/emit/watch Redis events for Franklin Lap Counter testing.

Mimics the referee and driver web apps by publishing to the same Redis channels
so you can test the GUI, TUI, scoreboard, and other components without a browser.

Usage:
  # Inject commands like the referee web app (to hardware:in)
  franklin-inject.py inject start_race --total-laps 10 --race-mode FAKE
  franklin-inject.py inject add_penalty --racer-id 3 --penalty-seconds 5
  franklin-inject.py inject disqualify_racer --racer-id 2

  # Emit hardware events like the Rust monitor (to hardware:out)
  franklin-inject.py emit lap --racer-id 1 --sensor-id 1
  franklin-inject.py emit start_race

  # Watch all Redis traffic
  franklin-inject.py watch
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from redis_commands import build_command_envelope, parse_command_envelope


HARDWARE_IN = "hardware:in"
HARDWARE_OUT = "hardware:out"
EVENTS = "franklin:events"
RACE_STATE = "franklin:race_state"

ALL_CHANNELS = [HARDWARE_IN, HARDWARE_OUT, EVENTS, RACE_STATE]

DEFAULT_SOURCE = "cli_tool"


def _get_redis_socket(args: argparse.Namespace) -> str:
    return args.redis_socket or os.environ.get(
        "FRANKLIN_REDIS_SOCKET", "./redis.sock"
    )


def _connect(args: argparse.Namespace) -> Any:
    import redis as redis_mod

    sock = _get_redis_socket(args)
    try:
        return redis_mod.Redis(unix_socket_path=sock, decode_responses=True)
    except Exception as exc:
        print(f"Error: cannot connect to Redis at {sock}: {exc}", file=sys.stderr)
        sys.exit(1)


def _parse_keyval_pairs(args: list[str]) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    i = 0
    while i < len(args):
        if args[i].startswith("--"):
            key = args[i].lstrip("-").replace("-", "_")
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                val: Any = args[i + 1]
                try:
                    val = int(val)
                except ValueError:
                    try:
                        val = float(val)
                    except ValueError:
                        pass
                kwargs[key] = val
                i += 2
            else:
                kwargs[key] = True
                i += 1
        else:
            i += 1
    return kwargs


# --- inject subcommand ---

def cmd_inject(args: argparse.Namespace, extra: list[str]) -> None:
    redis = _connect(args)
    command = args.command
    fields = _parse_keyval_pairs(extra)

    envelope = build_command_envelope(command, source=args.source, **fields)
    validated = parse_command_envelope(envelope)
    payload = json.dumps(validated)

    redis.publish(HARDWARE_IN, payload)
    print(f"Published command '{command}' to {HARDWARE_IN}")
    print(json.dumps(validated, indent=2))


# --- emit subcommand ---

LAP_TEMPLATE: dict[str, Any] = {
    "racer_id": 1,
    "sensor_id": 1,
    "lap_number": 1,
    "race_start_at": 0.0,
    "lap_at": 0.0,
    "recorded_at": 0.0,
    "simulated": True,
}

START_RACE_TEMPLATE: dict[str, Any] = {
    "at": 0.0,
    "recorded_at": 0.0,
    "command_id": "",
    "source": DEFAULT_SOURCE,
    "simulated": True,
}

HEARTBEAT_TEMPLATE: dict[str, Any] = {
    "recorded_at": 0.0,
    "simulated": True,
}

EVENT_TEMPLATES: dict[str, dict[str, Any]] = {
    "lap": LAP_TEMPLATE,
    "start_race": START_RACE_TEMPLATE,
    "heartbeat": HEARTBEAT_TEMPLATE,
}


def cmd_emit(args: argparse.Namespace, extra: list[str]) -> None:
    redis = _connect(args)
    event_type = args.event_type
    fields = _parse_keyval_pairs(extra)

    now = time.time()

    base = EVENT_TEMPLATES.get(event_type, {})
    payload: dict[str, Any] = {"type": event_type}
    payload.update(base)
    payload.update(fields)

    if "recorded_at" in payload and payload["recorded_at"] == 0.0:
        payload["recorded_at"] = now
    if event_type == "lap":
        if payload.get("lap_at", 0.0) == 0.0:
            payload["lap_at"] = now
        if payload.get("race_start_at", 0.0) == 0.0:
            payload["race_start_at"] = now - 10.0
        if payload.get("simulated") is None:
            payload["simulated"] = True
    elif event_type == "start_race":
        if payload.get("at", 0.0) == 0.0:
            payload["at"] = now
        if not payload.get("command_id"):
            payload["command_id"] = str(uuid4())
        if not payload.get("source"):
            payload["source"] = args.source
    elif event_type == "heartbeat":
        pass

    payload_str = json.dumps(payload)
    redis.publish(HARDWARE_OUT, payload_str)
    print(f"Published event '{event_type}' to {HARDWARE_OUT}")
    print(json.dumps(payload, indent=2))


# --- watch subcommand ---

CHANNEL_ALIASES: dict[str, str] = {
    "in": HARDWARE_IN,
    "hardware:in": HARDWARE_IN,
    "out": HARDWARE_OUT,
    "hardware:out": HARDWARE_OUT,
    "events": EVENTS,
    "franklin:events": EVENTS,
    "race_state": RACE_STATE,
    "franklin:race_state": RACE_STATE,
}


def cmd_watch(args: argparse.Namespace, _extra: list[str]) -> None:
    import redis as redis_mod

    sock = _get_redis_socket(args)

    raw = [c.strip() for c in args.channels.split(",")]
    channels = [CHANNEL_ALIASES[c] if c in CHANNEL_ALIASES else c for c in raw]
    if "all" in channels:
        channels = ALL_CHANNELS

    try:
        redis = redis_mod.Redis(unix_socket_path=sock, decode_responses=True)
        pubsub = redis.pubsub()
        for ch in channels:
            pubsub.subscribe(ch)
    except Exception as exc:
        print(f"Error: cannot subscribe at {sock}: {exc}", file=sys.stderr)
        sys.exit(1)

    ch_labels = ", ".join(channels)
    print(f"Watching {ch_labels} (Ctrl+C to stop)")
    print("-" * 50)

    shutdown = False

    def _signal_handler(_sig: int, _frame: Any) -> None:
        nonlocal shutdown
        shutdown = True

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        for message in pubsub.listen():
            if shutdown:
                break
            if message["type"] != "message":
                continue
            channel = message.get("channel", "?")
            data = message.get("data", "")
            ts = datetime.now(UTC).strftime("%H:%M:%S.%f")[:-3]
            try:
                parsed = json.loads(data)
                pretty = json.dumps(parsed, indent=2)
            except (json.JSONDecodeError, TypeError):
                pretty = str(data)
            print(f"[{ts}] [{channel}] {pretty}")
            print("-" * 50)
    except KeyboardInterrupt:
        pass
    finally:
        pubsub.close()
        redis.close()


# --- main ---

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inject/emit/watch Redis events for Franklin Lap Counter testing."
    )
    parser.add_argument(
        "--redis-socket",
        default="",
        help="Path to Redis unix socket (default: FRANKLIN_REDIS_SOCKET env or ./redis.sock)",
    )
    parser.add_argument(
        "--source",
        default=DEFAULT_SOURCE,
        help=f"Source identifier for commands (default: {DEFAULT_SOURCE})",
    )

    sub = parser.add_subparsers(dest="subcommand", required=True)

    inject = sub.add_parser("inject", help="Publish a command to hardware:in (like referee web app)")
    inject.add_argument("command", help="Command name (start_race, end_race, pause_race, resume_race, reset_race, add_penalty, remove_lap, disqualify_racer, update_contestant_name)")
    inject.set_defaults(func=cmd_inject)

    emit = sub.add_parser("emit", help="Publish an event to hardware:out (simulate hardware)")
    emit.add_argument("event_type", help="Event type (lap, start_race, heartbeat, status, error)")
    emit.set_defaults(func=cmd_emit)

    watch = sub.add_parser("watch", help="Subscribe to Redis channels and display live traffic")
    watch.add_argument(
        "--channels",
        default="all",
        help=f"Comma-separated channels to watch (default: all). Options: {', '.join(ALL_CHANNELS)}",
    )
    watch.set_defaults(func=cmd_watch)

    return parser


def main() -> None:
    parser = build_parser()
    args, extra = parser.parse_known_args()
    args.func(args, extra)


if __name__ == "__main__":
    main()
