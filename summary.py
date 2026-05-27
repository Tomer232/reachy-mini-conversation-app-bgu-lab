"""Per-conversation summary generator.

Reads ``conversation_dir/events.jsonl`` and writes ``summary.json`` next
to it. Tolerates malformed lines (logged-and-skipped via the
``reachy.summary`` logger). Surfaces known bug patterns — currently:

  - Multi-tool-call turns that hung (no end_of_turn after tool dispatch)
  - Missing ``main.shutdown`` event (cancellation cascade)

CLI: ``python -m summary [--conversation <id-or-latest>]``
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from pathlib import Path
from typing import Any


log = logging.getLogger("reachy.summary")


# ---------- public API ----------------------------------------------------

def generate_summary(conversation_dir: Path) -> dict:
    conversation_dir = Path(conversation_dir)
    events_path = conversation_dir / "events.jsonl"
    convo_id = conversation_dir.name

    if not events_path.exists():
        return {"error": "events.jsonl missing", "conversation_id": convo_id}

    events: list[dict] = []
    malformed = 0
    for i, raw in enumerate(events_path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            events.append(json.loads(raw))
        except Exception as e:
            malformed += 1
            log.warning("events.jsonl line %d malformed: %s", i, e)

    if not events:
        result = {
            "error": "events.jsonl empty or all malformed",
            "conversation_id": convo_id,
            "malformed_count": malformed,
        }
        _write(conversation_dir / "summary.json", result)
        return result

    summary = _build(convo_id, events, malformed)
    _write(conversation_dir / "summary.json", summary)
    return summary


# ---------- internals ----------------------------------------------------

_BLANK_TURN: dict[str, Any] = {
    "turn_id": None,
    "user_speech_ms": None,
    "gemini_first_chunk_ms": None,
    "total_ms": None,
    "samples_sent_to_robot": None,
    "tool_calls_count": 0,
    "tool_calls_suppressed": 0,
    "drain_timeouts": 0,
    "hard_aborts": 0,
    "end_of_turn_received": False,
    "aborted": False,
    "abort_reason": None,
    "errors": [],
}


def _new_turn(tid: int) -> dict[str, Any]:
    t = dict(_BLANK_TURN)
    t["errors"] = []
    t["turn_id"] = tid
    return t


def _build(convo_id: str, events: list[dict], malformed: int) -> dict:
    first, last = events[0], events[-1]
    started_at = first.get("t_iso", "")
    ended_at = last.get("t_iso", "")
    total_duration_s = max(
        0.0,
        (last.get("t_mono_ms", 0) - first.get("t_mono_ms", 0)) / 1000.0,
    )

    config: dict = {}
    calibration: dict | None = None
    exit_info = {"shutdown_emitted": False, "reason": None}
    for e in events:
        n = e.get("event")
        if n == "main.startup":
            config = dict(e.get("flags", {}))
        elif n == "calibration.final":
            calibration = {
                "rms": e.get("rms"),
                "peak": e.get("peak"),
                "threshold": e.get("threshold"),
            }
        elif n == "main.shutdown":
            exit_info["shutdown_emitted"] = True
            exit_info["reason"] = e.get("reason")

    turns: dict[int, dict] = {}

    # turn.start sites also pair the most recent user.speech.end (which
    # carries turn_id=null per the accepted Phase 2 deviation).
    for ts in events:
        if ts.get("event") != "turn.start":
            continue
        tid = ts.get("turn_id")
        if tid is None:
            continue
        t = turns.setdefault(tid, _new_turn(tid))
        t_start_mono = ts.get("t_mono_ms", 0)
        for e in reversed(events):
            if (e.get("event") == "user.speech.end"
                    and e.get("t_mono_ms", 0) <= t_start_mono):
                ms = e.get("duration_ms")
                if ms is not None:
                    t["user_speech_ms"] = int(ms)
                break

    # Attribute everything else by turn_id.
    error_event_names = {"gemini.session.error", "player.error"}
    for e in events:
        tid = e.get("turn_id")
        n = e.get("event") or ""
        if tid is None:
            continue
        t = turns.setdefault(tid, _new_turn(tid))
        if n == "gemini.recv.first_chunk":
            ms = e.get("latency_ms_from_user_speech_end")
            if ms is not None:
                t["gemini_first_chunk_ms"] = int(ms)
        elif n == "gemini.recv.end_of_turn":
            t["end_of_turn_received"] = True
        elif n == "turn.end":
            if e.get("total_ms") is not None:
                t["total_ms"] = int(e["total_ms"])
            if e.get("samples_sent_to_robot") is not None:
                t["samples_sent_to_robot"] = int(e["samples_sent_to_robot"])
            if e.get("aborted"):
                t["aborted"] = True
                t["abort_reason"] = e.get("reason")
        elif n == "transport.sentinel.sent" and e.get("sentinel_name") == "MOTION":
            t["tool_calls_count"] += 1
        elif n == "tool.suppressed":
            t["tool_calls_suppressed"] += 1
        elif n == "drain.timeout":
            t["drain_timeouts"] += 1
        elif n == "drain.hard_abort":
            t["hard_aborts"] += 1
        if n in error_event_names or n.endswith(".error"):
            t["errors"].append(e)

    # Implicit abort: turn.start but no turn.end and no end_of_turn.
    # Covers older runs whose drain hung without the CancelledError
    # arm emitting turn.end (Phase 2 pre-2.5 conversation dirs).
    for t in turns.values():
        if (not t["end_of_turn_received"]
                and not t["aborted"]
                and t["total_ms"] is None):
            t["aborted"] = True
            t["abort_reason"] = "no_turn_end_observed"

    per_turn = [turns[tid] for tid in sorted(turns.keys())]
    total = len(per_turn)
    completed = sum(1 for t in per_turn if t["end_of_turn_received"])
    aborted = sum(1 for t in per_turn if t["aborted"])

    latencies = [t["gemini_first_chunk_ms"] for t in per_turn
                 if t["gemini_first_chunk_ms"] is not None]
    latency_stats = _percentile_stats(latencies) if latencies else {"n": 0}

    errors = [
        e for e in events
        if (e.get("event") in error_event_names
            or (e.get("event") or "").endswith(".error"))
    ]
    transport_errors_count = sum(
        1 for e in events
        if "transport" in (e.get("event") or "")
        and ("error" in (e.get("event") or "") or "fail" in (e.get("event") or ""))
    )

    drain_timeouts_count = sum(
        1 for e in events if e.get("event") == "drain.timeout"
    )
    hard_aborts_count = sum(
        1 for e in events if e.get("event") == "drain.hard_abort"
    )
    session_reopens_count = sum(
        1 for e in events if e.get("event") == "gemini.session.reopen"
    )
    anomalies = {
        "missing_main_shutdown": not exit_info["shutdown_emitted"],
        "turns_hung_after_tools": [
            t["turn_id"] for t in per_turn
            if t["tool_calls_count"] > 0 and not t["end_of_turn_received"]
        ],
        "drain_timeouts_count": drain_timeouts_count,
        "hard_aborts_count": hard_aborts_count,
        "session_reopens_count": session_reopens_count,
    }

    out = {
        "conversation_id": convo_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "total_duration_s": round(total_duration_s, 3),
        "config": config,
        "calibration": calibration,
        "exit": exit_info,
        "turn_count": {
            "total": total,
            "completed": completed,
            "aborted": aborted,
        },
        "per_turn": per_turn,
        "latency": {"gemini_first_chunk_ms": latency_stats},
        "anomalies": anomalies,
        "errors": errors,
        "transport_errors_count": transport_errors_count,
    }
    if malformed:
        out["malformed_event_lines"] = malformed
    return out


def _percentile_stats(values: list[int]) -> dict:
    vs = sorted(values)
    return {
        "min": int(vs[0]),
        "p25": int(round(_percentile(vs, 25))),
        "median": int(round(statistics.median(vs))),
        "p75": int(round(_percentile(vs, 75))),
        "max": int(vs[-1]),
        "n": len(vs),
    }


def _percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolation percentile, numpy-default semantics."""
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = (len(sorted_values) - 1) * (q / 100.0)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


def _write(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False),
                    encoding="utf-8")


# ---------- CLI ----------------------------------------------------------

def _conversations_root() -> Path:
    return Path(__file__).parent / "conversations"


def _resolve_conversation(arg: str) -> Path:
    root = _conversations_root()
    if arg == "latest":
        dirs = [p for p in root.iterdir() if p.is_dir()] if root.exists() else []
        if not dirs:
            raise SystemExit(f"No conversations under {root}")
        return max(dirs, key=lambda p: p.stat().st_mtime)
    p = Path(arg)
    if p.is_absolute() or (p.parts and p.parts[0] == "conversations"):
        return p
    return root / arg


def _human_header(summary: dict) -> str:
    cid = summary.get("conversation_id", "?")
    tc = summary.get("turn_count", {})
    exit_info = summary.get("exit", {})
    lat = summary.get("latency", {}).get("gemini_first_chunk_ms", {})
    anom = summary.get("anomalies", {})
    set_flags = [
        k for k, v in anom.items()
        if v not in (False, None, [], "", 0)
    ]
    parts = [
        f"Conversation {cid}",
        f"turns: total={tc.get('total', '?')} "
        f"completed={tc.get('completed', '?')} "
        f"aborted={tc.get('aborted', '?')}",
        f"exit: reason={exit_info.get('reason')!r} "
        f"shutdown_emitted={exit_info.get('shutdown_emitted')}",
    ]
    if lat.get("n", 0) > 0:
        parts.append(
            f"first-chunk latency: median={lat.get('median')}ms "
            f"(n={lat['n']}, range {lat.get('min')}-{lat.get('max')})"
        )
    else:
        parts.append("first-chunk latency: no data (n=0)")
    if set_flags:
        parts.append(f"anomalies: {set_flags}")
    return " | ".join(parts)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--conversation", default="latest",
                   help="Conversation id under conversations/, or 'latest'.")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s %(message)s")

    convo = _resolve_conversation(args.conversation)
    summary = generate_summary(convo)

    # The missing-events.jsonl path doesn't write summary.json; print and bail.
    if "error" in summary and not summary.get("turn_count"):
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 1

    print(_human_header(summary))
    print()
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
