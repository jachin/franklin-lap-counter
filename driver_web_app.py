#!/usr/bin/env python3
"""Driver/team web app server.

Provides a racer-focused view:
- pick a racer
- see start-light state mirrored from race timeline events
- view mode-specific racer details (practice mode currently implemented)

Authoritative channel/message reference:
- docs/redis-message-reference.md
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import redis.asyncio as redis
from aiohttp import web  # type: ignore[import-untyped]

from database import LapDatabase

# Redis contract reference: docs/redis-message-reference.md
REDIS_SOCKET_PATH = "./redis.sock"
REDIS_OUT_CHANNEL = "hardware:out"
REDIS_EVENTS_CHANNEL = "franklin:events"
WEB_PORT = 8083
WEB_HOST = "0.0.0.0"
STATIC_DIR = Path(__file__).parent / "static"
DB_PATH = "franklin.db"
CONFIG_PATH = Path(__file__).parent / "franklin.config.json"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class DriverWebAppServer:
    def __init__(
        self,
        redis_socket: str = REDIS_SOCKET_PATH,
        port: int = WEB_PORT,
        host: str = WEB_HOST,
        db_path: str = DB_PATH,
    ) -> None:
        self.redis_socket = redis_socket
        self.port = port
        self.host = host
        self.db_path = db_path
        self.app: web.Application = web.Application()
        self.redis_client: redis.Redis | None = None  # type: ignore[type-arg]
        self.redis_pubsub: Any | None = None
        self.websockets: set[web.WebSocketResponse] = set()
        self.db = LapDatabase(db_path)

        self.app.router.add_get("/", self.index_handler)
        self.app.router.add_get("/ws", self.websocket_handler)
        self.app.router.add_get("/api/config", self.get_config)
        self.app.router.add_get("/api/current", self.get_current)
        self.app.router.add_get(
            "/api/current/racers/{racer_id}/laps", self.get_current_racer_laps
        )

    async def index_handler(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "driver.html")

    async def websocket_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        self.websockets.add(ws)
        await ws.send_json(
            {
                "type": "connected",
                "message": "Driver WebSocket connected",
            }
        )

        try:
            async for _msg in ws:
                pass
        finally:
            self.websockets.discard(ws)

        return ws

    def _read_config(self) -> dict[str, Any]:
        try:
            config = {}
            for key in [
                "race_mode",
                "total_laps",
                "race_end_mode",
                "contestants",
                "last_race_contestant_ids",
                "racer_color_assignments",
            ]:
                val = self.db.get_preference(key)
                if val is not None:
                    config[key] = val

            # One-time migration fallback if database has no preferences
            if not config:
                if os.path.exists(CONFIG_PATH):
                    with open(CONFIG_PATH, "r") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        for k, v in loaded.items():
                            self.db.set_preference(k, v)
                            config[k] = v
                        return config

            if "total_laps" not in config:
                config["total_laps"] = 10
            if "contestants" not in config:
                config["contestants"] = []
            return config
        except Exception as exc:
            logger.error("Error reading config from database: %s", exc)
            return {"total_laps": 10, "contestants": []}

    async def get_config(self, request: web.Request) -> web.Response:
        return web.json_response(self._read_config())

    def _infer_mode(self, race: dict[str, Any] | None) -> str:
        if not race:
            return "unknown"

        notes = str(race.get("notes") or "")
        notes_lower = notes.lower()

        if "training mode" in notes_lower:
            return "practice"
        return "race"

    def _latest_race(self) -> dict[str, Any] | None:
        assert self.db.conn is not None
        cursor = self.db.conn.cursor()
        cursor.execute(
            """
            SELECT * FROM races
            ORDER BY COALESCE(start_at, strftime('%s', start_time)) DESC
            LIMIT 1
            """
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def _contestant_name_map(self) -> dict[int, str]:
        config = self._read_config()
        contestants = config.get("contestants")
        if not isinstance(contestants, list):
            return {}

        by_id: dict[int, str] = {}
        for entry in contestants:
            if not isinstance(entry, dict):
                continue

            id_raw = entry.get("transmitter_id")
            name_raw = entry.get("name")
            if not isinstance(name_raw, str):
                continue
            if not isinstance(id_raw, int):
                continue
            by_id[id_raw] = name_raw

        return by_id

    def _lap_elapsed_seconds(self, lap: dict[str, Any]) -> float | None:
        lap_at = lap.get("lap_at")
        race_start_at = lap.get("race_start_at")
        if isinstance(lap_at, (int, float)) and isinstance(race_start_at, (int, float)):
            return float(lap_at) - float(race_start_at)

        race_time = lap.get("race_time")
        if isinstance(race_time, (int, float)):
            return float(race_time)

        return None

    def _coerce_int(self, value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _build_race_mode_summary(
        self,
        *,
        race_id: int,
        names_by_id: dict[int, str],
        total_laps: int,
    ) -> dict[str, Any]:
        laps = self.db.get_race_laps(race_id)
        actions = self.db.get_race_control_actions(race_id=race_id)

        penalties_seconds: dict[int, int] = {}
        disqualified_racers: set[int] = set()

        # DB rows are newest-first; apply oldest->newest.
        for action in reversed(actions):
            if not bool(action.get("accepted")):
                continue

            command = str(action.get("command") or "")
            racer_id_raw = action.get("racer_id")
            if not isinstance(racer_id_raw, int):
                continue

            racer_id = int(racer_id_raw)
            if command == "add_penalty":
                penalty = self._coerce_int(action.get("penalty_seconds"), default=0)
                if penalty > 0:
                    penalties_seconds[racer_id] = (
                        penalties_seconds.get(racer_id, 0) + penalty
                    )
            elif command == "disqualify_racer":
                disqualified_racers.add(racer_id)

        laps_by_racer: dict[int, list[dict[str, Any]]] = {}
        for lap in laps:
            racer_id_raw = lap.get("racer_id")
            if not isinstance(racer_id_raw, int):
                continue
            racer_id = int(racer_id_raw)
            laps_by_racer.setdefault(racer_id, []).append(lap)

        racer_ids = set(names_by_id.keys())
        racer_ids.update(laps_by_racer.keys())
        racer_ids.update(penalties_seconds.keys())
        racer_ids.update(disqualified_racers)

        leaderboard_rows: list[dict[str, Any]] = []
        for racer_id in sorted(racer_ids):
            racer_laps = sorted(
                laps_by_racer.get(racer_id, []),
                key=lambda lap: (
                    (elapsed := self._lap_elapsed_seconds(lap)) is None,
                    elapsed if elapsed is not None else 0.0,
                ),
            )

            lap_count = len(
                [
                    lap
                    for lap in racer_laps
                    if self._coerce_int(lap.get("lap_number"), default=0) > 0
                ]
            )

            lap_times = [
                float(lap_time)
                for lap_time in (lap.get("lap_time") for lap in racer_laps)
                if isinstance(lap_time, (int, float))
            ]
            best_lap_time = min(lap_times) if lap_times else None
            last_lap_time = lap_times[-1] if lap_times else None

            elapsed_values = [
                elapsed
                for elapsed in (self._lap_elapsed_seconds(lap) for lap in racer_laps)
                if isinstance(elapsed, float)
            ]
            elapsed_total = max(elapsed_values) if elapsed_values else 0.0

            penalty_seconds = penalties_seconds.get(racer_id, 0)
            adjusted_total_seconds = elapsed_total + float(penalty_seconds)
            is_disqualified = racer_id in disqualified_racers

            leaderboard_rows.append(
                {
                    "racer_id": racer_id,
                    "name": names_by_id.get(racer_id, f"Driver {racer_id}"),
                    "lap_count": lap_count,
                    "best_lap_time": best_lap_time,
                    "last_lap_time": last_lap_time,
                    "elapsed_total_seconds": elapsed_total,
                    "penalty_seconds": penalty_seconds,
                    "adjusted_total_seconds": adjusted_total_seconds,
                    "is_disqualified": is_disqualified,
                }
            )

        active_rows = [row for row in leaderboard_rows if not row["is_disqualified"]]
        dq_rows = [row for row in leaderboard_rows if row["is_disqualified"]]

        active_rows.sort(
            key=lambda row: (
                -int(row["lap_count"]),
                float(row["adjusted_total_seconds"]),
                (
                    float(row["best_lap_time"])
                    if isinstance(row["best_lap_time"], (int, float))
                    else float("inf")
                ),
                int(row["racer_id"]),
            )
        )

        for idx, row in enumerate(active_rows, start=1):
            row["position"] = idx
            row["position_label"] = str(idx)

        dq_rows.sort(key=lambda row: int(row["racer_id"]))
        for row in dq_rows:
            row["position"] = None
            row["position_label"] = "DQ"

        leader_row = active_rows[0] if active_rows else None
        leader_racer_id = int(leader_row["racer_id"]) if leader_row else None

        for row in active_rows:
            if leader_row is None:
                row["laps_behind_leader"] = None
                row["gap_to_leader_seconds"] = None
                continue

            if int(row["racer_id"]) == int(leader_row["racer_id"]):
                row["laps_behind_leader"] = 0
                row["gap_to_leader_seconds"] = 0.0
                continue

            row["laps_behind_leader"] = max(
                0, int(leader_row["lap_count"]) - int(row["lap_count"])
            )
            row["gap_to_leader_seconds"] = max(
                0.0,
                float(row["adjusted_total_seconds"])
                - float(leader_row["adjusted_total_seconds"]),
            )

        for row in dq_rows:
            row["laps_behind_leader"] = None
            row["gap_to_leader_seconds"] = None

        return {
            "total_laps_target": total_laps,
            "leader_racer_id": leader_racer_id,
            "leaderboard": active_rows + dq_rows,
        }

    async def get_current(self, request: web.Request) -> web.Response:
        try:
            latest_race = self._latest_race()
            if latest_race is None:
                return web.json_response(
                    {
                        "race": None,
                        "mode": "unknown",
                        "racers": [],
                        "race_mode_summary": None,
                    }
                )

            race_id = int(latest_race["id"])
            stats = self.db.get_race_stats(race_id)
            names_by_id = self._contestant_name_map()
            config = self._read_config()
            total_laps = self._coerce_int(config.get("total_laps"), default=10)

            racer_ids = set(names_by_id.keys())
            racer_ids.update(int(k) for k in stats.keys())

            racers: list[dict[str, Any]] = []
            for racer_id in sorted(racer_ids):
                row = stats.get(racer_id, {})
                racers.append(
                    {
                        "racer_id": racer_id,
                        "name": names_by_id.get(racer_id, f"Driver {racer_id}"),
                        "lap_count": int(row.get("lap_count") or 0),
                        "best_lap_time": row.get("best_lap_time"),
                    }
                )

            mode = self._infer_mode(latest_race)
            race_mode_summary = (
                self._build_race_mode_summary(
                    race_id=race_id,
                    names_by_id=names_by_id,
                    total_laps=total_laps,
                )
                if mode == "race"
                else None
            )

            return web.json_response(
                {
                    "race": latest_race,
                    "mode": mode,
                    "racers": racers,
                    "race_mode_summary": race_mode_summary,
                }
            )
        except Exception as exc:
            logger.error("Error getting current race data: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

    async def get_current_racer_laps(self, request: web.Request) -> web.Response:
        try:
            racer_id = int(request.match_info["racer_id"])
            if racer_id <= 0:
                return web.json_response({"error": "Invalid racer_id"}, status=400)

            limit_raw = request.query.get("limit", "10")
            limit = int(limit_raw)
            if limit <= 0 or limit > 200:
                return web.json_response(
                    {"error": "limit must be between 1 and 200"}, status=400
                )

            latest_race = self._latest_race()
            if latest_race is None:
                return web.json_response(
                    {
                        "race": None,
                        "racer_id": racer_id,
                        "laps": [],
                    }
                )

            race_id = int(latest_race["id"])

            assert self.db.conn is not None
            cursor = self.db.conn.cursor()
            cursor.execute(
                """
                SELECT * FROM laps
                WHERE race_id = ? AND racer_id = ?
                ORDER BY COALESCE(lap_at, recorded_at, strftime('%s', timestamp), id) DESC
                LIMIT ?
                """,
                (race_id, racer_id, limit),
            )
            rows = [dict(row) for row in cursor.fetchall()]

            # Return oldest->newest for display readability.
            rows.reverse()

            return web.json_response(
                {
                    "race": latest_race,
                    "racer_id": racer_id,
                    "laps": rows,
                }
            )
        except ValueError:
            return web.json_response({"error": "Invalid numeric parameter"}, status=400)
        except Exception as exc:
            logger.error("Error getting racer laps: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

    async def broadcast_to_websockets(self, data: dict[str, Any]) -> None:
        if not self.websockets:
            return

        disconnected: set[web.WebSocketResponse] = set()
        for ws in self.websockets:
            try:
                await ws.send_json(data)
            except Exception:
                disconnected.add(ws)

        self.websockets -= disconnected

    async def redis_listener(self) -> None:
        try:
            self.redis_client = redis.Redis(
                unix_socket_path=self.redis_socket,
                decode_responses=True,
            )
            self.redis_pubsub = self.redis_client.pubsub()
            await self.redis_pubsub.subscribe(REDIS_OUT_CHANNEL, REDIS_EVENTS_CHANNEL)

            logger.info(
                "Driver app subscribed to Redis channels: %s, %s",
                REDIS_OUT_CHANNEL,
                REDIS_EVENTS_CHANNEL,
            )

            while True:
                message = await self.redis_pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
                if message and message.get("type") == "message":
                    data = message.get("data")
                    if isinstance(data, str):
                        try:
                            parsed = json.loads(data)
                            if isinstance(parsed, dict):
                                await self.broadcast_to_websockets(parsed)
                        except json.JSONDecodeError:
                            logger.error("Invalid JSON from Redis: %s", data)

                await asyncio.sleep(0.01)
        finally:
            if self.redis_pubsub:
                await self.redis_pubsub.aclose()
            if self.redis_client:
                await self.redis_client.aclose()

    async def start_background_tasks(self, app: web.Application) -> None:
        app["redis_listener_task"] = asyncio.create_task(self.redis_listener())

    async def cleanup_background_tasks(self, app: web.Application) -> None:
        app["redis_listener_task"].cancel()
        await app["redis_listener_task"]
        self.db.close()

    def run(self) -> None:
        self.app.on_startup.append(self.start_background_tasks)
        self.app.on_cleanup.append(self.cleanup_background_tasks)
        logger.info("Starting driver web app on http://%s:%d", self.host, self.port)
        web.run_app(self.app, host=self.host, port=self.port)


def main() -> None:
    DriverWebAppServer().run()


if __name__ == "__main__":
    main()
