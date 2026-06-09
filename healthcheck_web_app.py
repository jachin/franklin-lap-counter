#!/usr/bin/env python3
"""Franklin health-check web app.

Serves a small dashboard and JSON report endpoint intended to be proxied by
Caddy at healthcheck.frank.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import redis.asyncio as redis
from aiohttp import web  # type: ignore[import-untyped]

REDIS_SOCKET_PATH = "./redis.sock"
REDIS_OUT_CHANNEL = "hardware:out"
WEB_PORT = 8082
WEB_HOST = "0.0.0.0"
STATIC_DIR = Path(__file__).parent / "static"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class HealthCheckWebAppServer:
    def __init__(
        self,
        redis_socket: str = REDIS_SOCKET_PATH,
        port: int = WEB_PORT,
        host: str = WEB_HOST,
    ) -> None:
        self.redis_socket = redis_socket
        self.port = port
        self.host = host
        self.app = web.Application()

        self.app.router.add_get("/", self.index_handler)
        self.app.router.add_get("/api/health", self.health_handler)
        self.app.router.add_get("/api/report", self.report_handler)

    async def index_handler(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "healthcheck.html")

    async def health_handler(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "service": "healthcheck_web_app"})

    def _run_command(
        self, command: list[str], timeout_seconds: int = 5
    ) -> dict[str, Any]:
        try:
            proc = subprocess.run(  # noqa: S603
                command,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            return {
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "stdout": proc.stdout.strip(),
                "stderr": proc.stderr.strip(),
            }
        except Exception as exc:  # pragma: no cover - defensive
            return {"ok": False, "error": str(exc)}

    def _tail_file(self, path: Path, lines: int = 20) -> str:
        if not path.exists():
            return "(missing)"

        try:
            content = path.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(content[-lines:])
        except Exception as exc:  # pragma: no cover - defensive
            return f"(failed to read: {exc})"

    def _check_http(self, url: str, host: str | None = None) -> dict[str, Any]:
        import urllib.request

        req = urllib.request.Request(url)
        if host is not None:
            req.add_header("Host", host)

        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                status = response.status
                body = response.read(200).decode("utf-8", "replace")
            return {"ok": 200 <= status < 400, "status": status, "body_preview": body}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def _sample_heartbeat(self) -> dict[str, Any]:
        if not Path(self.redis_socket).exists():
            return {
                "ok": False,
                "error": f"redis socket not found at {self.redis_socket}",
            }

        client = redis.Redis(unix_socket_path=self.redis_socket, decode_responses=True)
        pubsub = client.pubsub()

        try:
            await pubsub.subscribe(REDIS_OUT_CHANNEL)
            deadline = asyncio.get_event_loop().time() + 6.0

            while asyncio.get_event_loop().time() < deadline:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
                if not message:
                    await asyncio.sleep(0.05)
                    continue

                data = message.get("data")
                if not isinstance(data, str):
                    continue

                try:
                    parsed = json.loads(data)
                except json.JSONDecodeError:
                    continue

                if isinstance(parsed, dict) and parsed.get("type") == "heartbeat":
                    return {"ok": True, "sample": parsed}

            return {"ok": False, "error": "no heartbeat observed in 6 seconds"}
        finally:
            await pubsub.aclose()
            await client.aclose()

    async def _build_report(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []

        checks.append(
            {
                "name": "terminfo_xterm_ghostty",
                "result": self._run_command(["infocmp", "-x", "xterm-ghostty"]),
            }
        )

        checks.append(
            {
                "name": "tmux_sessions",
                "result": self._run_command(["tmux", "ls"]),
            }
        )

        checks.append(
            {
                "name": "redis_ping",
                "result": self._run_command(
                    ["redis-cli", "-s", self.redis_socket, "PING"]
                ),
            }
        )

        checks.append(
            {"name": "heartbeat_sample", "result": await self._sample_heartbeat()}
        )

        checks.append(
            {
                "name": "referee_process",
                "result": self._run_command(["pgrep", "-af", "referee_web_app.py"]),
            }
        )

        checks.append(
            {
                "name": "wayvnc_process",
                "result": self._run_command(["pgrep", "-af", "wayvnc"]),
            }
        )

        checks.append(
            {
                "name": "emoji_font",
                "result": self._run_command(
                    ["sh", "-lc", "fc-list | grep -qi 'Noto Color Emoji'"]
                ),
            }
        )

        checks.append(
            {
                "name": "scoreboard_direct_http",
                "result": self._check_http("http://127.0.0.1:8080/"),
            }
        )

        checks.append(
            {
                "name": "referee_direct_http",
                "result": self._check_http("http://127.0.0.1:8081/api/health"),
            }
        )

        checks.append(
            {
                "name": "caddy_scoreboard_proxy",
                "result": self._check_http(
                    "http://127.0.0.1/",
                    host="scoreboard.frank",
                ),
            }
        )

        checks.append(
            {
                "name": "caddy_referee_proxy",
                "result": self._check_http(
                    "http://127.0.0.1/api/health",
                    host="referee.frank",
                ),
            }
        )

        checks.append(
            {
                "name": "caddy_healthcheck_proxy",
                "result": self._check_http(
                    "http://127.0.0.1/api/health",
                    host="healthcheck.frank",
                ),
            }
        )

        if Path("gui.log").exists():
            gui_tail = self._tail_file(Path("gui.log"), lines=120)
            gui_issues = [
                line
                for line in gui_tail.splitlines()
                if "Traceback" in line
                or "TypeError" in line
                or "Redis connect failed" in line
                or "ERROR" in line
            ]
            checks.append(
                {
                    "name": "gui_log_recent_issues",
                    "result": {
                        "ok": len(gui_issues) == 0,
                        "matches": gui_issues,
                    },
                }
            )

        checks.append(
            {
                "name": "hardware_redis_log_tail",
                "result": {
                    "ok": True,
                    "tail": self._tail_file(Path("hardware_redis.log"), lines=20),
                },
            }
        )

        caddy_active = self._run_command(["systemctl", "is-active", "caddy.service"])
        checks.append({"name": "caddy_service", "result": caddy_active})

        report_ok = all(bool(check["result"].get("ok", False)) for check in checks)

        return {
            "ok": report_ok,
            "generated_at": datetime.now(UTC).isoformat(),
            "host": socket.gethostname(),
            "cwd": os.getcwd(),
            "checks": checks,
        }

    async def report_handler(self, request: web.Request) -> web.Response:
        report = await self._build_report()
        status = 200 if report["ok"] else 503
        return web.json_response(report, status=status)

    def run(self) -> None:
        logger.info(
            "Starting health-check web app on http://%s:%d", self.host, self.port
        )
        web.run_app(self.app, host=self.host, port=self.port)


def main() -> None:
    HealthCheckWebAppServer().run()


if __name__ == "__main__":
    main()
