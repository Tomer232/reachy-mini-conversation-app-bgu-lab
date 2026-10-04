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

from conversation import SCRIPT_DIR, ROBOT_HOST_DEFAULT
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
    p.add_argument("--robot-host", default=None,
                   help="Robot SSH host. Overrides the REACHY_ROBOT_HOST env "
                        f"var (default {ROBOT_HOST_DEFAULT}).")
    # --- fleet: who this instance is, and on whose key ---
    # All four are what the hub passes at launch (docs/HUB-INTERFACE.md). All
    # four are optional, because a hand-started robot must still come up.
    p.add_argument("--robot-id", default=None,
                   help="This robot's hardware id (Pollen unit_id). The hub "
                        "discovers it over mDNS and passes it in; cached in "
                        "robot_identity.json for hand-starts.")
    p.add_argument("--robot-name", default=None,
                   help="The name this robot is known by. Assigned once and "
                        "then permanent; shown on the dashboard and recorded "
                        "in every conversation.")
    p.add_argument("--provider", default=None,
                   help="Speech backend: gemini (default) or gpt_live.")
    p.add_argument("--api-key-id", default=None,
                   help="Which key in keys.json this robot talks through. The "
                        "hub owns the robots-to-keys map and passes the id.")
    p.add_argument("--api-key", default=None,
                   help="A literal key, bypassing keys.json. Prefer "
                        "--api-key-id; a key on a command line is a key in "
                        "every process list on the machine.")
    # --- microphone ---
    # The K11 lavalier is gone; every robot listens through its own built-in
    # mic now, whose device name and rate have not been measured on hardware.
    # tools/probe_internal_mic.py prints both.
    p.add_argument("--mic-match", default=None,
                   help="Case-insensitive substring of the capture device "
                        "name. Overrides the built-in default (and the "
                        "REACHY_MIC_MATCH env var).")
    p.add_argument("--mic-rate", type=int, default=None,
                   help="Capture rate in Hz for robot mode, when the built-in "
                        "mic will not open at the default 48000.")
    p.add_argument("--local-robot", action="store_true",
                   help="Robot mode: this process is running ON the robot with "
                        "the K11 receiver in the robot's USB. Captures the "
                        "robot's mic, runs the player as a child process "
                        "instead of over SSH, and uses the onnx VAD. Pair with "
                        "--host 0.0.0.0 --no-browser so the dashboard is "
                        "reachable from the laptop.")
    return p.parse_args(argv)


def _log_tag(name: "str | None") -> str:
    """A filename-safe suffix from a robot's name. Empty when unnamed, so a
    single-robot checkout keeps the filenames it has always had."""
    if not name:
        return ""
    safe = "".join(ch if (ch.isalnum() or ch in "-_") else "-"
                   for ch in str(name).strip())
    safe = safe.strip("-")[:32]
    return f"_{safe}" if safe else ""


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
        # Both boards: running a lecture needs the show board too, and /show is
        # not a path anyone recalls under pressure. The gap gives a cold-start
        # browser time to come up — a second open fired at a browser that is
        # still launching gets dropped. Show board last so it ends up focused.
        for i, u in enumerate((url, url.rstrip("/") + "/show")):
            if i:
                await asyncio.sleep(1.5)
            try:
                webbrowser.open(u)
            except Exception:
                log.warning("Could not auto-open browser; visit %s", u)
    await serve_task


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Set before anything reads it. conversation.py consults LOCAL_ROBOT at
    # call time rather than capturing it at import, so flipping it here (after
    # the module is imported) reaches every branch that depends on it.
    if args.local_robot:
        import conversation as conv_mod
        conv_mod.LOCAL_ROBOT = True

    # Mic overrides, same reasoning: conversation.py reads these at call time,
    # so setting them here (after import) reaches the resolver.
    if args.mic_match or args.mic_rate:
        import conversation as conv_mod
        if args.mic_match:
            conv_mod.INPUT_DEVICE_LINUX = args.mic_match
            conv_mod.INPUT_DEVICE = args.mic_match
        if args.mic_rate:
            conv_mod.CAPTURE_RATE_LINUX = args.mic_rate
            conv_mod.CAPTURE_DECIM_LINUX = max(
                1, args.mic_rate // conv_mod.GEMINI_INPUT_RATE)

    # 1. Base logging -> per-startup system log.
    #
    # The robot's name goes in the filename. On a robot this is redundant —
    # each body has its own disk — but system logs get copied off robots and
    # mailed around, and ten files called 2026-09-17_10-04-11.log are ten
    # files nobody can tell apart afterwards.
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    tag = _log_tag(args.robot_name or args.robot_id)
    system_log_path = SCRIPT_DIR / "system_logs" / f"{ts}{tag}.log"
    init_base_logging(system_log_path)

    # 2. Broadcaster + WS log bridge.
    broadcaster = Broadcaster()
    add_handler(WebSocketLogHandler(broadcaster), level=logging.INFO)

    # 3. SystemManager + app.
    manager = SystemManager(broadcaster, robot_host=args.robot_host,
                            robot_id=args.robot_id,
                            robot_name=args.robot_name,
                            provider=args.provider,
                            api_key_id=args.api_key_id,
                            api_key=args.api_key)
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
