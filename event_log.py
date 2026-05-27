"""JSONL event log for the reachy_chat laptop process.

One ``EventLogger`` per run writes structured events to
``conversation_dir / "events.jsonl"``. Each call to ``log_event(name, **fields)``
appends one JSON object on its own line. Lock-protected, flushed-on-write.

The schema for every line is exactly:

    t_iso            ISO-8601 local time with offset, ms precision
    t_mono_ms        int(time.monotonic() * 1000)
    conversation_id  str (constant for the run)
    turn_id          int | None (set/cleared by the caller around each turn)
    event            str
    <all kwargs>     flattened at top level (no "fields" wrapper)

Defensive: any ``bytes``/``bytearray`` field value longer than 64 bytes
raises ``ValueError`` immediately. This is on purpose — it stops a stray
audio buffer from landing in the log. Callers should wrap ``log_event``
in try/except so a logging bug never crashes the run; the class itself
does NOT swallow.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any


_BYTES_FIELD_LIMIT = 64
_RESERVED_KEYS = ("t_iso", "t_mono_ms", "conversation_id", "turn_id", "event")


def _json_default(o: Any) -> Any:
    if isinstance(o, (bytes, bytearray, memoryview)):
        return f"<bytes len={len(o)}>"
    if isinstance(o, Path):
        return str(o)
    raise TypeError(f"not JSON-serializable: {type(o).__name__}")


def _iso_local_ms() -> str:
    now = datetime.now().astimezone()
    ms = now.microsecond // 1000
    base = now.strftime("%Y-%m-%dT%H:%M:%S")
    off = now.strftime("%z")
    if off and len(off) == 5:
        off = off[:3] + ":" + off[3:]
    return f"{base}.{ms:03d}{off}"


class EventLogger:
    """JSONL event sink. Thread-safe. One open append fd per instance."""

    def __init__(self, conversation_dir: Path, conversation_id: str) -> None:
        self._path = Path(conversation_dir) / "events.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conversation_id = str(conversation_id)
        self._turn_id: int | None = None
        self._lock = threading.Lock()
        self._fh = self._path.open("a", encoding="utf-8")

    @property
    def path(self) -> Path:
        return self._path

    @property
    def turn_id(self) -> int | None:
        return self._turn_id

    def set_turn(self, turn_id: int) -> None:
        self._turn_id = int(turn_id)

    def clear_turn(self) -> None:
        self._turn_id = None

    def log_event(self, event: str, **fields: Any) -> None:
        for k, v in fields.items():
            if isinstance(v, (bytes, bytearray, memoryview)) and len(v) > _BYTES_FIELD_LIMIT:
                raise ValueError(
                    f"event {event!r} field {k!r}: bytes value of length {len(v)} "
                    f"exceeds {_BYTES_FIELD_LIMIT}-byte safety limit "
                    "(audio buffers must not be logged)"
                )

        record: dict[str, Any] = {
            "t_iso": _iso_local_ms(),
            "t_mono_ms": int(time.monotonic() * 1000),
            "conversation_id": self._conversation_id,
            "turn_id": self._turn_id,
            "event": str(event),
        }
        # Caller fields go on top so they appear in the line; reserved keys
        # are protected (a buggy caller cannot rewrite event/turn_id).
        for k, v in fields.items():
            if k in _RESERVED_KEYS:
                continue
            record[k] = v

        line = json.dumps(record, ensure_ascii=False, default=_json_default)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                try:
                    self._fh.flush()
                finally:
                    self._fh.close()
