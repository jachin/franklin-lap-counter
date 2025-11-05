#!/usr/bin/env python3
"""
WebSocket server that bridges Redis pub/sub to WebSocket clients.
Subscribes to hardware:out Redis channel and broadcasts events to all connected WebSocket clients.
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any
from datetime import datetime

import redis.asyncio as redis
from aiohttp import web  # type: ignore[import-untyped]

from database import LapDatabase

# Configuration
REDIS_SOCKET_PATH = "./redis.sock"
REDIS_OUT_CHANNEL = "hardware:out"
WEB_PORT = 8080
STATIC_DIR = Path(__file__).parent / "static"
DB_PATH = "lap_counter.db"
CONFIG_PATH = Path(__file__).parent / "config.json"

# Logging setup
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class WebSocketServer:
    def __init__(
        self,
        redis_socket: str = REDIS_SOCKET_PATH,
        port: int = WEB_PORT,
        db_path: str = DB_PATH,
    ) -> None:
        self.redis_socket: str = redis_socket
        self.port: int = port
        self.db_path: str = db_path
        self.app: web.Application = web.Application()
        self.redis_client: redis.Redis | None = None  # type: ignore[type-arg]
        self.redis_pubsub: Any | None = None
        self.websockets: set[web.WebSocketResponse] = set()
        self.db: LapDatabase = LapDatabase(db_path)

        # Setup routes
        self.app.router.add_get("/ws", self.websocket_handler)
        self.app.router.add_get("/", self.index_handler)

        # REST API routes
        self.app.router.add_get("/api/races", self.get_races)
        self.app.router.add_get("/api/races/{race_id}/laps", self.get_race_laps)
        self.app.router.add_get("/api/races/{race_id}/stats", self.get_race_stats)
        self.app.router.add_get("/api/config", self.get_config)
        self.app.router.add_get("/api/debug/simulate/{event_type}", self.debug_simulate)

        self.app.router.add_static("/static", STATIC_DIR, name="static")

    async def index_handler(self, request: web.Request) -> web.FileResponse:
        """Serve the index.html file"""
        index_file = STATIC_DIR / "index.html"
        return web.FileResponse(index_file)

    async def get_races(self, request: web.Request) -> web.Response:
        """Get paginated list of races ordered by newest to oldest"""
        try:
            # Get pagination parameters
            page = int(request.query.get("page", "1"))
            limit = int(request.query.get("limit", "10"))

            # Validate parameters
            if page < 1:
                return web.json_response({"error": "Page must be >= 1"}, status=400)
            if limit < 1 or limit > 100:
                return web.json_response(
                    {"error": "Limit must be between 1 and 100"}, status=400
                )

            offset = (page - 1) * limit

            # Get races from database
            assert self.db.conn is not None
            cursor = self.db.conn.cursor()

            # Get total count
            cursor.execute("SELECT COUNT(*) as total FROM races")
            total = cursor.fetchone()["total"]

            # Get paginated races
            cursor.execute(
                """
                SELECT * FROM races
                ORDER BY start_time DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
            races = [dict(row) for row in cursor.fetchall()]

            return web.json_response(
                {
                    "races": races,
                    "pagination": {
                        "page": page,
                        "limit": limit,
                        "total": total,
                        "total_pages": (total + limit - 1) // limit,
                    },
                }
            )
        except ValueError:
            return web.json_response(
                {"error": "Invalid pagination parameters"}, status=400
            )
        except Exception as e:
            logger.error(f"Error getting races: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def get_race_laps(self, request: web.Request) -> web.Response:
        """Get all laps for a specific race"""
        try:
            race_id = int(request.match_info["race_id"])
            laps = self.db.get_race_laps(race_id)
            return web.json_response({"race_id": race_id, "laps": laps})
        except ValueError:
            return web.json_response({"error": "Invalid race ID"}, status=400)
        except Exception as e:
            logger.error(f"Error getting race laps: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def get_race_stats(self, request: web.Request) -> web.Response:
        """Get statistics for a specific race"""
        try:
            race_id = int(request.match_info["race_id"])
            stats = self.db.get_race_stats(race_id)
            return web.json_response({"race_id": race_id, "stats": stats})
        except ValueError:
            return web.json_response({"error": "Invalid race ID"}, status=400)
        except Exception as e:
            logger.error(f"Error getting race stats: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def websocket_handler(self, request: web.Request) -> web.WebSocketResponse:
        """Handle WebSocket connections"""
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        self.websockets.add(ws)
        logger.info(
            f"WebSocket client connected. Total clients: {len(self.websockets)}"
        )

        try:
            # Send initial connection confirmation
            await ws.send_json({"type": "connected", "message": "WebSocket connected"})

            # Keep connection alive and handle incoming messages (if any)
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    logger.debug(f"Received from client: {msg.data}")
                elif msg.type == web.WSMsgType.ERROR:
                    logger.error(f"WebSocket error: {ws.exception()}")
        finally:
            self.websockets.discard(ws)
            logger.info(
                f"WebSocket client disconnected. Total clients: {len(self.websockets)}"
            )

        return ws

    async def get_config(self, request: web.Request) -> web.Response:
        """Get the configuration from config.json"""
        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r") as f:
                    config = json.load(f)
                return web.json_response(config)
            else:
                logger.error(f"Config file not found: {CONFIG_PATH}")
                return web.json_response(
                    {"error": "Configuration file not found"}, status=404
                )
        except Exception as e:
            logger.error(f"Error reading config file: {e}")
            return web.json_response(
                {"error": f"Error reading configuration: {str(e)}"}, status=500
            )

    async def debug_simulate(self, request: web.Request) -> web.Response:
        """Debug endpoint to simulate events for testing"""
        event_type = request.match_info.get("event_type", "")

        if event_type == "race_start":
            event_data = {"type": "status", "message": "Race started"}
            await self.broadcast_to_websockets(event_data)
            return web.json_response(
                {"success": True, "message": "Simulated race start event"}
            )

        elif event_type == "race_end":
            event_data = {"type": "status", "message": "Race ended"}
            await self.broadcast_to_websockets(event_data)
            return web.json_response(
                {"success": True, "message": "Simulated race end event"}
            )

        elif event_type == "lap":
            event_data = {
                "type": "lap",
                "racer_id": 1,
                "sensor_id": 1,
                "race_time": 10.5,
            }
            await self.broadcast_to_websockets(event_data)
            return web.json_response(
                {"success": True, "message": "Simulated lap event"}
            )

        else:
            return web.json_response(
                {"error": f"Unknown event type: {event_type}"}, status=400
            )

    async def broadcast_to_websockets(self, data: dict[str, Any]) -> None:
        """Broadcast data to all connected WebSocket clients"""
        if not self.websockets:
            return

        # Remove disconnected websockets
        disconnected = set()
        for ws in self.websockets:
            try:
                await ws.send_json(data)
            except Exception as e:
                logger.error(f"Error sending to WebSocket: {e}")
                disconnected.add(ws)

        # Clean up disconnected clients
        self.websockets -= disconnected

    async def redis_listener(self) -> None:
        """Listen to Redis pub/sub and broadcast to WebSocket clients"""
        try:
            self.redis_client = redis.Redis(
                unix_socket_path=self.redis_socket, decode_responses=True
            )
            self.redis_pubsub = self.redis_client.pubsub()
            await self.redis_pubsub.subscribe(REDIS_OUT_CHANNEL)

            logger.info(f"Subscribed to Redis channel: {REDIS_OUT_CHANNEL}")

            while True:
                message = await self.redis_pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message and message["type"] == "message":
                    try:
                        data = json.loads(message["data"])
                        logger.debug(
                            f"Broadcasting to {len(self.websockets)} clients: {data.get('type', 'unknown')}"
                        )
                        await self.broadcast_to_websockets(data)
                    except json.JSONDecodeError:
                        logger.error(f"Invalid JSON from Redis: {message['data']}")

                await asyncio.sleep(0.01)

        except Exception as e:
            logger.error(f"Redis listener error: {e}")
        finally:
            if self.redis_pubsub:
                await self.redis_pubsub.aclose()
            if self.redis_client:
                await self.redis_client.aclose()

    async def start_background_tasks(self, app: web.Application) -> None:
        """Start background tasks when app starts"""
        app["redis_listener_task"] = asyncio.create_task(self.redis_listener())

    async def cleanup_background_tasks(self, app: web.Application) -> None:
        """Cleanup background tasks when app stops"""
        app["redis_listener_task"].cancel()
        await app["redis_listener_task"]

    def run(self) -> None:
        """Start the web server"""
        # Setup startup/cleanup
        self.app.on_startup.append(self.start_background_tasks)
        self.app.on_cleanup.append(self.cleanup_background_tasks)

        logger.info(f"Starting WebSocket server on http://localhost:{self.port}")
        logger.info(f"Serving static files from: {STATIC_DIR}")

        web.run_app(self.app, port=self.port)


def main() -> None:
    """Main entry point"""
    server = WebSocketServer()
    server.run()


if __name__ == "__main__":
    main()
