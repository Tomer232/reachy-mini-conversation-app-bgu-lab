"""Centralized logging configuration for the reachy_chat laptop process.

Single entry point: ``setup_logging(conversation_dir)``. Wires the named
``"reachy"`` logger (NOT the root logger) with two handlers:

  - console handler  -> stderr, level from env REACHY_LOG_CONSOLE (default INFO)
  - file handler     -> conversation_dir / "laptop.log", level from env
                        REACHY_LOG_FILE (default DEBUG)

Both handlers run records through:
  - a ``monotonic_ms`` filter that stamps ``record.monotonic_ms``
  - a redacting formatter that masks Gemini API keys and HF tokens in the
    final formatted output (defense-in-depth; we don't intentionally log
    secrets, but if a stack trace or repr leaks one it gets masked here).

Component logger names are conventional (see laptop_chat.py): each module
calls ``logging.getLogger("reachy.<component>.<sub>")`` and the handlers
on the parent ``"reachy"`` logger pick them up via propagation.

Laptop-side component loggers (Phase 2 instrumentation):

  reachy.main                  top-level startup, shutdown, turn boundaries
  reachy.audio.capture         mic frame reads, sd callbacks
  reachy.audio.vad             VAD / energy gate / hangover decisions
  reachy.audio.calibration     calibration samples + final
  reachy.gemini.session        live session open/close/keepalive/error
  reachy.gemini.stream         audio chunks to/from Gemini, text receipts
  reachy.transport.ssh         SSH connection lifecycle: connect, ready,
                               exit rc, transport errors (NOT robot voice)
  reachy.transport.framing     4-byte BE length framing, sentinel encode/decode
  reachy.robot.stderr          stderr drainer — forwards robot-side log lines
                               (named separately from transport.ssh so the
                               robot's voice stays greppable on its own)
  reachy.state                 state-machine transitions
  reachy.motion                motion-related laptop-side bookkeeping
  reachy.tools                 tool dispatch (Phase 4 baseline: inert)

Robot-side loggers (Phase 3, configured by robot_streaming_player.py):

  reachy.robot.player          startup, playback, shutdown
  reachy.robot.transport       framing reader, sentinel decode
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from pathlib import Path


_REDACT_PATTERNS = (
    re.compile(r"AIzaSy[0-9A-Za-z_-]{33}"),
    re.compile(r"hf_[0-9A-Za-z]{34,}"),
)
_REDACT_REPLACEMENT = "***REDACTED***"


class _MonotonicMsFilter(logging.Filter):
    """Inject ``record.monotonic_ms`` so the file formatter can render it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.monotonic_ms = int(time.monotonic() * 1000)
        return True


class _RedactingMsFormatter(logging.Formatter):
    """Formatter with millisecond-precision timestamps and secret redaction.

    ``time_style`` controls the asctime format:
      - "short" -> HH:MM:SS.mmm   (console)
      - "iso"   -> YYYY-MM-DDTHH:MM:SS.mmm   (file)

    Redaction runs on the fully-formatted string, so it covers both ``msg``
    and any args/exception text the parent formatter pulled in.
    """

    def __init__(self, fmt: str, time_style: str = "iso") -> None:
        super().__init__(fmt=fmt)
        if time_style not in ("short", "iso"):
            raise ValueError(f"time_style must be 'short' or 'iso', got {time_style!r}")
        self._time_style = time_style

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        ct = self.converter(record.created)
        if self._time_style == "short":
            base = time.strftime("%H:%M:%S", ct)
        else:
            base = time.strftime("%Y-%m-%dT%H:%M:%S", ct)
        return f"{base}.{int(record.msecs):03d}"

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for pat in _REDACT_PATTERNS:
            text = pat.sub(_REDACT_REPLACEMENT, text)
        return text


def _console_formatter() -> _RedactingMsFormatter:
    return _RedactingMsFormatter(
        fmt="%(asctime)s %(levelname)-5s %(name)-28s %(message)s",
        time_style="short",
    )


def _file_formatter() -> _RedactingMsFormatter:
    return _RedactingMsFormatter(
        fmt="%(asctime)s mono=%(monotonic_ms)d %(levelname)-5s %(name)-28s %(threadName)-18s %(message)s",
        time_style="iso",
    )


def setup_logging(
    conversation_dir: Path,
    console_level: str | None = None,
    file_level: str = "DEBUG",
) -> None:
    """Configure the ``reachy`` logger tree. Idempotent — safe to re-call.

    Legacy single-shot configuration used by the old linear CLI flow: wipes
    all handlers and wires console + one conversation-scoped laptop.log. The
    dashboard does NOT use this; it uses ``init_base_logging`` plus
    ``open_conversation_log`` / ``close_conversation_log`` instead, so the
    base (console + system file + WS) handlers survive across conversations.
    Kept for the archived CLI and any tools that still import it.
    """
    if console_level is None:
        console_level = os.environ.get("REACHY_LOG_CONSOLE", "INFO")
    file_level = os.environ.get("REACHY_LOG_FILE", file_level)

    logger = logging.getLogger("reachy")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

    mono = _MonotonicMsFilter()

    console = logging.StreamHandler(stream=sys.stderr)
    console.setLevel(_resolve_level(console_level, default=logging.INFO))
    console.setFormatter(_console_formatter())
    console.addFilter(mono)
    logger.addHandler(console)

    log_path = Path(conversation_dir) / "laptop.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # delay=True -> file is opened on first emit, per Phase-1 acceptance criterion.
    file_h = logging.FileHandler(log_path, encoding="utf-8", delay=True)
    file_h.setLevel(_resolve_level(file_level, default=logging.DEBUG))
    file_h.setFormatter(_file_formatter())
    file_h.addFilter(mono)
    logger.addHandler(file_h)


# === Dashboard logging API =========================================
#
# The dashboard keeps a persistent base handler set on the "reachy" logger
# for the whole process lifetime (console + a per-startup system log + an
# optional WS-broadcast handler). Per-conversation laptop.log handlers are
# attached when a conversation starts and detached when it ends — WITHOUT
# disturbing the base set. This is the key difference from setup_logging,
# which wipes every handler on each call.

# Marker attribute so we can tell base handlers apart from per-conversation
# ones when we need to reason about the handler list.
_BASE_MARKER = "_reachy_base_handler"
_CONV_MARKER = "_reachy_conversation_handler"


def init_base_logging(
    system_log_path: Path,
    console_level: str | None = None,
    file_level: str = "DEBUG",
) -> None:
    """Install the persistent base handlers on the ``reachy`` logger.

    Console -> stderr; system file -> ``system_log_path`` (one per startup).
    Wipes any pre-existing handlers (so re-calling is safe) and tags the
    ones it installs with ``_BASE_MARKER``. Call once at process startup,
    BEFORE any conversation exists, so VAD-load and robot-connect log lines
    land in the system log.
    """
    if console_level is None:
        console_level = os.environ.get("REACHY_LOG_CONSOLE", "INFO")
    file_level = os.environ.get("REACHY_LOG_FILE", file_level)

    logger = logging.getLogger("reachy")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

    mono = _MonotonicMsFilter()

    console = logging.StreamHandler(stream=sys.stderr)
    console.setLevel(_resolve_level(console_level, default=logging.INFO))
    console.setFormatter(_console_formatter())
    console.addFilter(mono)
    setattr(console, _BASE_MARKER, True)
    logger.addHandler(console)

    sys_path = Path(system_log_path)
    sys_path.parent.mkdir(parents=True, exist_ok=True)
    sys_h = logging.FileHandler(sys_path, encoding="utf-8", delay=True)
    sys_h.setLevel(_resolve_level(file_level, default=logging.DEBUG))
    sys_h.setFormatter(_file_formatter())
    sys_h.addFilter(mono)
    setattr(sys_h, _BASE_MARKER, True)
    logger.addHandler(sys_h)


def add_handler(handler: logging.Handler, level: str | int = logging.INFO) -> None:
    """Attach an extra persistent handler (e.g. the WS-broadcast bridge) to
    the ``reachy`` logger. Adds the monotonic filter and tags it as base."""
    handler.setLevel(_resolve_level(level, default=logging.INFO)
                     if isinstance(level, str) else level)
    handler.addFilter(_MonotonicMsFilter())
    setattr(handler, _BASE_MARKER, True)
    logging.getLogger("reachy").addHandler(handler)


def open_conversation_log(
    conversation_dir: Path,
    file_level: str = "DEBUG",
) -> logging.Handler:
    """Attach a per-conversation ``laptop.log`` file handler and return it.

    The base handlers stay in place, so robot stderr / state logs are
    mirrored to BOTH the system log and this conversation log while the
    conversation runs (the mirroring the plan explicitly allows). Pass the
    returned handler to ``close_conversation_log`` when the conversation ends.
    """
    file_level = os.environ.get("REACHY_LOG_FILE", file_level)
    log_path = Path(conversation_dir) / "laptop.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_h = logging.FileHandler(log_path, encoding="utf-8", delay=True)
    file_h.setLevel(_resolve_level(file_level, default=logging.DEBUG))
    file_h.setFormatter(_file_formatter())
    file_h.addFilter(_MonotonicMsFilter())
    setattr(file_h, _CONV_MARKER, True)
    logging.getLogger("reachy").addHandler(file_h)
    return file_h


def close_conversation_log(handler: logging.Handler | None) -> None:
    """Detach and close a per-conversation handler from ``open_conversation_log``."""
    if handler is None:
        return
    logger = logging.getLogger("reachy")
    try:
        logger.removeHandler(handler)
    finally:
        try:
            handler.close()
        except Exception:
            pass


def _resolve_level(level: str | int, default: int) -> int:
    if isinstance(level, int):
        return level
    if not level:
        return default
    resolved = logging.getLevelName(level.upper())
    return resolved if isinstance(resolved, int) else default
