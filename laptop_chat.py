#!/usr/bin/env python3
"""Reachy Mini × Gemini — Handler Dashboard entry point.

Phase A of the dashboard refactor. This file used to be the whole monolithic
CLI; that turn-loop logic now lives in conversation.py (Conversation.run) and
the lifetime resources + state machine live in system.py (SystemManager). The
pre-refactor monolith is preserved verbatim under
archive/snapshot_pre_dashboard/laptop_chat.py.

What this does now:

  1. Initialise base logging to system_logs/<startup-timestamp>.log (the idle
     system log; VAD-load and robot-connect lines land here).
  2. Build the Broadcaster and attach the WS log bridge to the `reachy` logger.
  3. Build the SystemManager and the FastAPI app.
  4. Start uvicorn on 127.0.0.1:<port> (default 8765); SystemManager.startup
     runs concurrently (STARTING -> IDLE_BREATHING) so the page is reachable
     immediately.
  5. Open the dashboard in the browser once the server is listening.

Ctrl-C in the terminal triggers uvicorn's graceful shutdown, which runs the
lifespan teardown: stop any conversation, then close SSH (EOF on stdin so the
robot player exits cleanly).

Run from your LAPTOP, not the robot:

    python laptop_chat.py            # default port 8765
    python laptop_chat.py --port 9000
"""

from __future__ import annotations

import sys
import asyncio
import logging
import argparse
import webbrowser
from datetime import datetime

# Force UTF-8 stdout so Hebrew prints on Windows
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import uvicorn

from conversation import SCRIPT_DIR
from logging_setup import init_base_logging, add_handler
from web.broadcaster import Broadcaster, WebSocketLogHandler
from system import SystemManager
from web.server import create_app


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

log = logging.getLogger("reachy.main")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Reachy Mini handler dashboard.")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"HTTP/WS port (default {DEFAULT_PORT}).")
    p.add_argument("--host", default=DEFAULT_HOST,
                   help=f"Bind address (default {DEFAULT_HOST}; localhost only).")
    p.add_argument("--no-browser", action="store_true",
                   help="Do not auto-open the dashboard in a browser.")
    return p.parse_args(argv)


async def _serve(app, host: str, port: int, open_browser: bool) -> None:
    config = uvicorn.Config(app, host=host, port=port, log_level="warning",
                            lifespan="on")
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())

    # Wait until the server is actually listening, then open the browser.
    while not server.started and not serve_task.done():
        await asyncio.sleep(0.1)
    url = f"http://{host}:{port}/"
    if serve_task.done():
        # Startup failed (e.g. port in use) — surface the error.
        await serve_task
        return
    log.info("Dashboard listening at %s", url)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            log.warning("Could not auto-open browser; visit %s", url)
    await serve_task


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # 1. Base logging -> per-startup system log.
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    system_log_path = SCRIPT_DIR / "system_logs" / f"{ts}.log"
    init_base_logging(system_log_path)

    # 2. Broadcaster + WS log bridge.
    broadcaster = Broadcaster()
    add_handler(WebSocketLogHandler(broadcaster), level=logging.INFO)

    # 3. SystemManager + app.
    manager = SystemManager(broadcaster)
    app = create_app(manager, broadcaster)

    log.info("Starting dashboard on %s:%d (system log: %s)",
             args.host, args.port, system_log_path)

    try:
        asyncio.run(_serve(app, args.host, args.port, not args.no_browser))
    except KeyboardInterrupt:
        # uvicorn normally handles SIGINT itself; this is a backstop.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
