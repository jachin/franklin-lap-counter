#!/usr/bin/env python3
"""
WebSocket server that bridges Redis pub/sub to WebSocket clients.
Subscribes to hardware:out Redis channel and broadcasts events to all connected WebSocket clients.
"""

import asyncio
import json
import logging
from pathlib import Path

import redis.asyncio as redis
from aiohttp import web

# Configuration
REDIS_SOCKET_PATH = "./redis.sock"
REDIS_OUT_CHANNEL = "hardware:out"
WEB_PORT = 8080
STATIC_DIR = Path(__file__).parent / "static"

# Logging setup
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class WebSocketServer:
    def __init__(self, redis_socket=REDIS_SOCKET_PATH, port=WEB_PORT):
        self.redis_socket = redis_socket
        self.port = port
        self.app = web.Application()
        self.redis_client = None
        self.redis_pubsub = None
        self.websockets = set()

        # Setup routes
        self.app.router.add_get("/ws", self.websocket_handler)
        self.app.router.add_get("/", self.index_handler)
        self.app.router.add_static("/static", STATIC_DIR, name="static")

    async def index_handler(self, request):
        """Serve the index.html file"""
        index_file = STATIC_DIR / "index.html"
        return web.FileResponse(index_file)

    async def websocket_handler(self, request):
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

    async def broadcast_to_websockets(self, data):
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

    async def redis_listener(self):
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
                await self.redis_pubsub.close()
            if self.redis_client:
                await self.redis_client.close()

    async def start_background_tasks(self, app):
        """Start background tasks when app starts"""
        app["redis_listener_task"] = asyncio.create_task(self.redis_listener())

    async def cleanup_background_tasks(self, app):
        """Cleanup background tasks when app stops"""
        app["redis_listener_task"].cancel()
        await app["redis_listener_task"]

    def run(self):
        """Start the web server"""
        # Setup startup/cleanup
        self.app.on_startup.append(self.start_background_tasks)
        self.app.on_cleanup.append(self.cleanup_background_tasks)

        logger.info(f"Starting WebSocket server on http://localhost:{self.port}")
        logger.info(f"Serving static files from: {STATIC_DIR}")

        web.run_app(self.app, port=self.port)


def main():
    """Main entry point"""
    server = WebSocketServer()
    server.run()


if __name__ == "__main__":
    main()
