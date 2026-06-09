#!/usr/bin/env python3
"""
Referee web app server.

Provides race-control REST endpoints and a WebSocket feed of race-control/hardware
messages to referee clients.

Authoritative channel/message reference:
- docs/redis-message-reference.md
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import redis.asyncio as redis
from aiohttp import web  # type: ignore[import-untyped]

from database import LapDatabase

# Redis contract reference: docs/redis-message-reference.md
REDIS_SOCKET_PATH = "./redis.sock"
REDIS_IN_CHANNEL = "hardware:in"
REDIS_OUT_CHANNEL = "hardware:out"
REDIS_EVENTS_CHANNEL = "franklin:events"
WEB_PORT = 8081
WEB_HOST = "0.0.0.0"
STATIC_DIR = Path(__file__).parent / "static"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class RefereeWebAppServer:
    def __init__(
        self,
        redis_socket: str = REDIS_SOCKET_PATH,
        port: int = WEB_PORT,
        host: str = WEB_HOST,
    ) -> None:
        self.redis_socket = redis_socket
        self.port = port
        self.host = host
        self.app: web.Application = web.Application()
        self.redis_client: redis.Redis | None = None  # type: ignore[type-arg]
        self.redis_pubsub: Any | None = None
        self.websockets: set[web.WebSocketResponse] = set()
        self.db = LapDatabase("lap_counter.db")

        self.app.router.add_get("/", self.index_handler)
        self.app.router.add_get("/ws", self.websocket_handler)
        self.app.router.add_get("/api/health", self.health_handler)

        self.app.router.add_post("/api/control/start_race", self.start_race_handler)
        self.app.router.add_post("/api/control/end_race", self.end_race_handler)
        self.app.router.add_post("/api/control/reset_race", self.reset_race_handler)
        self.app.router.add_post("/api/control/add_penalty", self.add_penalty_handler)
        self.app.router.add_post("/api/control/remove_lap", self.remove_lap_handler)
        self.app.router.add_post(
            "/api/control/disqualify_racer", self.disqualify_racer_handler
        )
        self.app.router.add_get("/api/control/audit", self.audit_handler)

    async def index_handler(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "referee.html")

    async def health_handler(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def websocket_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        self.websockets.add(ws)
        await ws.send_json(
            {
                "type": "connected",
                "message": "Referee WebSocket connected",
            }
        )

        try:
            async for _msg in ws:
                pass
        finally:
            self.websockets.discard(ws)

        return ws

    async def _publish_command(self, payload: dict[str, Any]) -> None:
        if not self.redis_client:
            raise RuntimeError("Redis not connected")

        payload.setdefault("type", "command")
        payload.setdefault("command_id", str(uuid4()))
        payload.setdefault("source", "referee_web_app")
        payload.setdefault("timestamp", datetime.now(UTC).isoformat())

        await self.redis_client.publish(REDIS_IN_CHANNEL, json.dumps(payload))

    async def start_race_handler(self, request: web.Request) -> web.Response:
        base = time.time() + 0.25
        ready_at = base
        set_at = base + 1.0
        go_at = base + 2.0
        payload = {
            "command": "start_race",
            "source": "referee_web_app",
            "timestamp": datetime.now(UTC).isoformat(),
            "ready_at": ready_at,
            "set_at": set_at,
            "go_at": go_at,
            "start_at": go_at,
        }
        await self._publish_command(payload)
        return web.json_response({"ok": True, "published": payload})

    async def end_race_handler(self, request: web.Request) -> web.Response:
        payload = {"command": "end_race"}
        await self._publish_command(payload)
        return web.json_response({"ok": True, "published": payload})

    async def reset_race_handler(self, request: web.Request) -> web.Response:
        payload = {"command": "reset_race"}
        await self._publish_command(payload)
        return web.json_response({"ok": True, "published": payload})

    async def add_penalty_handler(self, request: web.Request) -> web.Response:
        body = await request.json()

        racer_id = int(body.get("racer_id", 0))
        penalty_seconds = int(body.get("penalty_seconds", 5))
        reason = str(body.get("reason", ""))

        if racer_id <= 0:
            return web.json_response(
                {"ok": False, "error": "Invalid racer_id"}, status=400
            )

        if penalty_seconds <= 0 or penalty_seconds % 5 != 0:
            return web.json_response(
                {
                    "ok": False,
                    "error": "penalty_seconds must be a positive 5-second increment",
                },
                status=400,
            )

        payload = {
            "command": "add_penalty",
            "racer_id": racer_id,
            "penalty_seconds": penalty_seconds,
            "reason": reason,
        }
        await self._publish_command(payload)
        return web.json_response({"ok": True, "published": payload})

    async def remove_lap_handler(self, request: web.Request) -> web.Response:
        body = await request.json()

        racer_id = int(body.get("racer_id", 0))
        reason = str(body.get("reason", ""))
        lap_number_raw = body.get("lap_number")
        lap_number = int(lap_number_raw) if lap_number_raw not in (None, "") else None

        if racer_id <= 0:
            return web.json_response(
                {"ok": False, "error": "Invalid racer_id"}, status=400
            )

        if lap_number is not None and lap_number <= 0:
            return web.json_response(
                {"ok": False, "error": "lap_number must be > 0"},
                status=400,
            )

        payload: dict[str, Any] = {
            "command": "remove_lap",
            "racer_id": racer_id,
            "reason": reason,
        }
        if lap_number is not None:
            payload["lap_number"] = lap_number

        await self._publish_command(payload)
        return web.json_response({"ok": True, "published": payload})

    async def disqualify_racer_handler(self, request: web.Request) -> web.Response:
        body = await request.json()

        racer_id = int(body.get("racer_id", 0))
        reason = str(body.get("reason", ""))

        if racer_id <= 0:
            return web.json_response(
                {"ok": False, "error": "Invalid racer_id"}, status=400
            )

        payload = {
            "command": "disqualify_racer",
            "racer_id": racer_id,
            "reason": reason,
        }
        await self._publish_command(payload)
        return web.json_response({"ok": True, "published": payload})

    async def audit_handler(self, request: web.Request) -> web.Response:
        race_id_raw = request.query.get("race_id")
        limit_raw = request.query.get("limit", "100")

        race_id: int | None = None
        if race_id_raw not in (None, ""):
            try:
                race_id = int(race_id_raw)
            except ValueError:
                return web.json_response(
                    {"ok": False, "error": "race_id must be an integer"},
                    status=400,
                )
            if race_id <= 0:
                return web.json_response(
                    {"ok": False, "error": "race_id must be > 0"},
                    status=400,
                )

        try:
            limit = int(limit_raw)
        except ValueError:
            return web.json_response(
                {"ok": False, "error": "limit must be an integer"}, status=400
            )

        if limit <= 0:
            return web.json_response(
                {"ok": False, "error": "limit must be > 0"}, status=400
            )

        # Keep response bounded even if caller requests too much.
        limit = min(limit, 500)

        actions = self.db.get_race_control_actions(race_id=race_id)[:limit]
        return web.json_response(
            {"ok": True, "count": len(actions), "actions": actions}
        )

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

    def _infer_current_race_id_for_audit(self) -> int | None:
        in_progress = self.db.get_in_progress_race()
        if in_progress:
            race_id = in_progress.get("id")
            if isinstance(race_id, int):
                return race_id
        return None

    def _audit_race_control_event(self, msg: dict[str, Any]) -> None:
        command_raw = msg.get("command")
        if not isinstance(command_raw, str) or not command_raw:
            return

        accepted = bool(msg.get("accepted", False))
        race_id = self._infer_current_race_id_for_audit()

        try:
            self.db.add_race_control_action(
                command=command_raw,
                accepted=accepted,
                payload=msg,
                race_id=race_id,
            )
        except Exception as exc:
            logger.error("Failed to write race-control audit row: %s", exc)

    async def redis_listener(self) -> None:
        try:
            self.redis_client = redis.Redis(
                unix_socket_path=self.redis_socket,
                decode_responses=True,
            )
            self.redis_pubsub = self.redis_client.pubsub()
            await self.redis_pubsub.subscribe(REDIS_EVENTS_CHANNEL, REDIS_OUT_CHANNEL)

            logger.info(
                "Referee app subscribed to Redis channels: %s, %s",
                REDIS_EVENTS_CHANNEL,
                REDIS_OUT_CHANNEL,
            )

            while True:
                message = await self.redis_pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
                if message and message["type"] == "message":
                    data = message.get("data")
                    if isinstance(data, str):
                        try:
                            parsed = json.loads(data)
                            if isinstance(parsed, dict):
                                if parsed.get("type") == "race_control":
                                    self._audit_race_control_event(parsed)
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
        logger.info("Starting referee web app on http://%s:%d", self.host, self.port)
        web.run_app(self.app, host=self.host, port=self.port)


def main() -> None:
    RefereeWebAppServer().run()


if __name__ == "__main__":
    main()
