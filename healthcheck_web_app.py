#!/usr/bin/env python3
"""Franklin health-check web app.

Serves a dashboard and health endpoints intended to be proxied by Caddy at
healthcheck.frank.

Authoritative channel/message reference:
- docs/redis-message-reference.md
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
from typing import Any, Awaitable, Callable

import redis.asyncio as redis
from aiohttp import web  # type: ignore[import-untyped]

# Redis contract reference: docs/redis-message-reference.md
REDIS_SOCKET_PATH = "./redis.sock"
REDIS_OUT_CHANNEL = "hardware:out"
WEB_PORT = 8082
WEB_HOST = "0.0.0.0"
STATIC_DIR = Path(__file__).parent / "static"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

CheckRunner = Callable[[], Awaitable[dict[str, Any]]]


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

        self.check_runners: dict[str, CheckRunner] = {
            "terminfo_xterm_ghostty": self._check_terminfo_xterm_ghostty,
            "tmux_sessions": self._check_tmux_sessions,
            "redis_ping": self._check_redis_ping,
            "heartbeat_sample": self._check_heartbeat_sample,
            "referee_process": self._check_referee_process,
            "driver_process": self._check_driver_process,
            "wayvnc_process": self._check_wayvnc_process,
            "emoji_font": self._check_emoji_font,
            "scoreboard_direct_http": self._check_scoreboard_direct_http,
            "referee_direct_http": self._check_referee_direct_http,
            "driver_direct_http": self._check_driver_direct_http,
            "caddy_scoreboard_proxy": self._check_caddy_scoreboard_proxy,
            "caddy_referee_proxy": self._check_caddy_referee_proxy,
            "caddy_healthcheck_proxy": self._check_caddy_healthcheck_proxy,
            "caddy_driver_proxy": self._check_caddy_driver_proxy,
            "gui_log_recent_issues": self._check_gui_log_recent_issues,
            "hardware_redis_log_tail": self._check_hardware_redis_log_tail,
            "caddy_service": self._check_caddy_service,
        }

        self.app.router.add_get("/", self.index_handler)
        self.app.router.add_get("/api/health", self.health_handler)
        self.app.router.add_get("/api/report", self.report_handler)
        self.app.router.add_get("/api/checks", self.checks_handler)
        self.app.router.add_get("/api/check/{name}", self.check_handler)

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

    async def _run_command_async(
        self, command: list[str], timeout_seconds: int = 5
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._run_command, command, timeout_seconds)

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

    async def _check_http_async(
        self, url: str, host: str | None = None
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._check_http, url, host)

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
            deadline = asyncio.get_running_loop().time() + 6.0

            while asyncio.get_running_loop().time() < deadline:
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

    async def _run_named_check(self, name: str) -> dict[str, Any]:
        runner = self.check_runners.get(name)
        if not runner:
            return {"name": name, "result": {"ok": False, "error": "unknown check"}}

        result = await runner()
        return {"name": name, "result": result}

    async def _build_report(self, parallel: bool = False) -> dict[str, Any]:
        names = list(self.check_runners.keys())

        if parallel:
            checks = await asyncio.gather(
                *(self._run_named_check(name) for name in names)
            )
        else:
            checks: list[dict[str, Any]] = []
            for name in names:
                checks.append(await self._run_named_check(name))

        report_ok = all(bool(check["result"].get("ok", False)) for check in checks)

        return {
            "ok": report_ok,
            "generated_at": datetime.now(UTC).isoformat(),
            "host": socket.gethostname(),
            "cwd": os.getcwd(),
            "checks": checks,
        }

    async def checks_handler(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"ok": True, "checks": list(self.check_runners.keys())}
        )

    async def check_handler(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if name not in self.check_runners:
            return web.json_response(
                {"ok": False, "error": f"unknown check: {name}"},
                status=404,
            )

        check = await self._run_named_check(name)
        status = 200 if check["result"].get("ok") else 503
        return web.json_response(check, status=status)

    async def report_handler(self, request: web.Request) -> web.Response:
        mode = request.query.get("mode", "full").lower()
        parallel = mode == "parallel"
        report = await self._build_report(parallel=parallel)
        status = 200 if report["ok"] else 503
        return web.json_response(report, status=status)

    async def _check_terminfo_xterm_ghostty(self) -> dict[str, Any]:
        return await self._run_command_async(["infocmp", "-x", "xterm-ghostty"])

    async def _check_tmux_sessions(self) -> dict[str, Any]:
        return await self._run_command_async(["tmux", "ls"])

    async def _check_redis_ping(self) -> dict[str, Any]:
        return await self._run_command_async(
            ["redis-cli", "-s", self.redis_socket, "PING"]
        )

    async def _check_heartbeat_sample(self) -> dict[str, Any]:
        return await self._sample_heartbeat()

    async def _check_referee_process(self) -> dict[str, Any]:
        return await self._run_command_async(["pgrep", "-af", "referee_web_app.py"])

    async def _check_driver_process(self) -> dict[str, Any]:
        return await self._run_command_async(["pgrep", "-af", "driver_web_app.py"])

    async def _check_wayvnc_process(self) -> dict[str, Any]:
        return await self._run_command_async(["pgrep", "-af", "wayvnc"])

    async def _check_emoji_font(self) -> dict[str, Any]:
        return await self._run_command_async(
            ["sh", "-lc", "fc-list | grep -qi 'Noto Color Emoji'"]
        )

    async def _check_scoreboard_direct_http(self) -> dict[str, Any]:
        return await self._check_http_async("http://127.0.0.1:8085/")

    async def _check_referee_direct_http(self) -> dict[str, Any]:
        return await self._check_http_async("http://127.0.0.1:8081/api/health")

    async def _check_driver_direct_http(self) -> dict[str, Any]:
        return await self._check_http_async("http://127.0.0.1:8083/")

    async def _check_caddy_scoreboard_proxy(self) -> dict[str, Any]:
        return await self._check_http_async(
            "http://127.0.0.1/", host="scoreboard.frank"
        )

    async def _check_caddy_referee_proxy(self) -> dict[str, Any]:
        return await self._check_http_async(
            "http://127.0.0.1/api/health", host="referee.frank"
        )

    async def _check_caddy_healthcheck_proxy(self) -> dict[str, Any]:
        return await self._check_http_async(
            "http://127.0.0.1/api/health", host="healthcheck.frank"
        )

    async def _check_caddy_driver_proxy(self) -> dict[str, Any]:
        return await self._check_http_async("http://127.0.0.1/", host="racer.frank")

    async def _check_gui_log_recent_issues(self) -> dict[str, Any]:
        gui_tail = await asyncio.to_thread(self._tail_file, Path("gui.log"), 120)
        if gui_tail == "(missing)":
            return {"ok": True, "matches": []}

        gui_issues = [
            line
            for line in gui_tail.splitlines()
            if "Traceback" in line
            or "TypeError" in line
            or "Redis connect failed" in line
            or "ERROR" in line
        ]

        return {
            "ok": len(gui_issues) == 0,
            "matches": gui_issues,
        }

    async def _check_hardware_redis_log_tail(self) -> dict[str, Any]:
        tail = await asyncio.to_thread(self._tail_file, Path("hardware_redis.log"), 20)
        return {"ok": True, "tail": tail}

    async def _check_caddy_service(self) -> dict[str, Any]:
        return await self._run_command_async(
            ["systemctl", "is-active", "caddy.service"]
        )

    def run(self) -> None:
        logger.info(
            "Starting health-check web app on http://%s:%d", self.host, self.port
        )
        web.run_app(self.app, host=self.host, port=self.port)


def main() -> None:
    HealthCheckWebAppServer().run()


if __name__ == "__main__":
    main()
