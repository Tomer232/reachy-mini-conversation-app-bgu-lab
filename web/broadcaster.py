"""WebSocket fan-out + a logging bridge that streams the `reachy` logger to
connected browsers.

Broadcaster holds the set of connected WebSocket clients and pushes the same
message dict to all of them. Two entry points:

  - ``broadcast`` (coroutine): call from the event loop.
  - ``broadcast_threadsafe``: call from ANY thread (the robot stderr drainer
    and the mic-capture worker both log off-loop); it marshals onto the loop
    via ``loop.call_soon_threadsafe`` (Phase A adjustment C).

It also keeps a 200-line ring buffer of formatted log lines so a freshly
connected client's ``state.snapshot`` can include the recent log tail.

``WebSocketLogHandler`` is a ``logging.Handler`` attached to the ``reachy``
logger; on each record it appends to the ring buffer and broadcasts a
structured ``{"event":"log", ...}`` message.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque


class Broadcaster:
    def __init__(self, recent_maxlen: int = 200):
        self._clients: set = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._recent: deque[str] = deque(maxlen=recent_maxlen)

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # ----- client registry -----

    async def register(self, ws) -> None:
        self._clients.add(ws)

    def unregister(self, ws) -> None:
        self._clients.discard(ws)

    # ----- recent-log ring buffer -----

    def push_log(self, line: str) -> None:
        # deque.append is atomic in CPython, so this is safe to call from the
        # logging handler on any thread without taking the loop.
        self._recent.append(line)

    def recent_log(self) -> list[str]:
        return list(self._recent)

    # ----- broadcasting -----

    async def broadcast(self, message: dict) -> None:
        if not self._clients:
            return
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    def broadcast_threadsafe(self, message: dict) -> None:
        """Schedule a broadcast from any thread (or the loop itself)."""
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._schedule, message)
        except RuntimeError:
            # Loop already closed (shutdown race) — drop the message.
            pass

    def _schedule(self, message: dict) -> None:
        # Runs on the loop thread (via call_soon_threadsafe).
        asyncio.ensure_future(self.broadcast(message))


class WebSocketLogHandler(logging.Handler):
    """Bridge `reachy` log records to the dashboard: ring buffer + WS stream."""

    def __init__(self, broadcaster: Broadcaster):
        super().__init__()
        self.broadcaster = broadcaster

    @staticmethod
    def _short_ts(record: logging.LogRecord) -> str:
        ct = time.localtime(record.created)
        return f"{time.strftime('%H:%M:%S', ct)}.{int(record.msecs):03d}"

    def emit(self, record: logging.LogRecord) -> None:
        try:
            ts = self._short_ts(record)
            msg = record.getMessage()
            # Ring-buffer line mirrors the console-ish shape.
            self.broadcaster.push_log(
                f"{ts} {record.levelname:<5} {record.name:<28} {msg}")
            self.broadcaster.broadcast_threadsafe({
                "event": "log",
                "level": record.levelname,
                "logger": record.name,
                "msg": msg,
                "ts": ts,
            })
        except Exception:
            self.handleError(record)
