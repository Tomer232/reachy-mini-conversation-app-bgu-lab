#!/usr/bin/env python3
"""Conversation turn loop, extracted from the original laptop_chat.py main().

Phase A of the dashboard refactor. This module holds everything that used to
live in laptop_chat.py EXCEPT the process entry point:

  - all runtime config constants (moved here per open-question #2),
  - component loggers + the module-level event-log/state globals,
  - get_api_key, the motion-tool catalog + tool builders,
  - record_with_vad (now interruptible via a threading.Event),
  - StreamingRobotPlayer (owned for the system lifetime by SystemManager,
    but defined here because it shares the _emit global and frame protocol),
  - TurnTimings, ConversationDir (the old `Conversation`, renamed),
  - drain_one_turn_streaming,
  - Conversation: the NEW turn-loop class whose run() is the old main() body.

Design notes (Phase A, confirmed adjustments):
  - record_with_vad / StreamingRobotPlayer construction / robot.close run off
    the event loop via asyncio.to_thread in SystemManager. record_with_vad
    takes a threading.Event `should_stop` it checks each loop iteration so a
    long mic wait can be cut short the instant the handler clicks End.
  - The module globals (_EV, _STATE, _TOOLS_DISPATCHED_THIS_TURN,
    _RECORD_META) stay module-level. Conversation.run() owns _EV's lifetime:
    it assigns _EV on entry and clears it on exit. Full de-globalization is
    out of scope for Phase A.
  - startup flags are re-emitted into every conversation's events.jsonl
    (main.startup) so summary.json's config stays populated.
"""

from __future__ import annotations

import os
import re
import sys
import csv
import time
import base64
import struct
import asyncio
import logging
import threading
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Callable

import numpy as np
import sounddevice as sd
import soundfile as sf
import paramiko
from scipy.signal import resample_poly

try:
    from google import genai
    from google.genai import types
except ImportError:
    sys.exit("Missing google-genai. Run: pip install google-genai")

from event_log import EventLogger
from vad import SileroVAD


# === Config ========================================================

ROBOT_HOST = os.environ.get("ROBOT_HOST", "10.100.102.18")
ROBOT_USER = os.environ.get("ROBOT_USER", "pollen")
ROBOT_PASSWORD = os.environ.get("ROBOT_PASSWORD", "root")
ROBOT_PYTHON = "/venvs/mini_daemon/bin/python"
ROBOT_STREAMING_PLAYER = "/home/pollen/scripts/robot_streaming_player.py"
ROBOT_OUTPUT_RATE = 16000   # what the robot plays at
ROBOT_READY_TIMEOUT_S = 30   # how long to wait for the robot to come up

GEMINI_MODEL = "gemini-3.1-flash-live-preview"
GEMINI_INPUT_RATE = 16000   # what Gemini's input expects
GEMINI_OUTPUT_RATE = 24000  # what Gemini emits

# Voice. All Hebrew quality was good in archive/voice_*.wav. Swap to
# any of: Aoede, Kore, Charon, Puck, Leda.
GEMINI_VOICE = "Aoede"
GEMINI_LANGUAGE_CODE = "he-IL"

SYSTEM_PROMPT = (
    "אתה Reachy Mini, רובוט שולחני קטן וידידותי. ענה תמיד בעברית. "
    "תגובות קצרות, משפט או שניים. היה חם וסקרן. "
    "אם המשתמש אומר שהוא רוצה לסיים את השיחה, אמור פרידה חמה וקצרה. "
    # Phase 3B: motion-tool addendum. Tone: dampen over-usage.
    "אתה יכול לבצע תנועות ורגשות במהלך השיחה. השתמש בכלים "
    "play_emotion, dance, ו-move_head כשמתאים — לא בכל משפט. "
    "תנועה נכונה ברגע הנכון משדרגת את האינטראקציה. "
    # Cleanup-round addendum: soft cap, paired with the hard cap in
    # MAX_TOOL_CALLS_PER_TURN. Reduces wasted tool-call attempts that
    # would otherwise be suppressed client-side.
    "השתמש בכלי אחד לכל היותר בכל תשובה."
)

END_PHRASES = (
    # Hebrew
    "סיים שיחה",
    "תסיים שיחה",
    "להתראות",
    "ביי",
    "תפסיק",
    "תפסיקי",
    # English
    "end conversation",
    "end the conversation",
    "goodbye",
    "good bye",
    "stop",
)
_END_BOUNDARY_RE = re.compile(
    r"(?:^|[\s.,!?؟،;:'\"\(\)\[\]\-])(?:"
    + "|".join(re.escape(p) for p in END_PHRASES)
    + r")(?:$|[\s.,!?؟،;:'\"\(\)\[\]\-])",
    re.IGNORECASE,
)

# --- VAD (Silero) ---
SILERO_THRESHOLD = 0.5            # speech-prob threshold; Silero's recommended default
SILERO_FRAME_SIZE = 512           # samples per inference at 16 kHz = 32 ms.
                                  # Silero supports {256, 512, 1024, 1536}; 512 is the
                                  # sweet spot for live mic — low latency, stable probs.
# Outer-loop knobs (independent of the VAD model — endpointing, not detection):
SILENCE_HANGOVER_S = 0.8
MAX_TURN_S = 30
WAIT_FOR_SPEECH_S = 15
MIN_SPEECH_S = 0.3

# Whether motion tools (play_emotion, dance, move_head) are registered
# with Gemini Live. Captured in main.startup.flags so the telemetry
# never lies about what tools the model could invoke for a given run.
ENABLE_TOOL_CALLS = True

# Hard cap on how many tool calls we will dispatch per turn. Anything
# beyond this is suppressed (no MOTION sentinel, no FunctionResponse to
# Gemini — Gemini keeps talking, the suppressed call quietly no-ops on
# our side). Two reasons: (1) UX — chaining tools mid-turn is rarely
# what we want; (2) workaround for the multi-tool-call hang where
# Gemini Live forgets to emit turn_complete after >1 tools.
MAX_TOOL_CALLS_PER_TURN = 1

# Watchdog timeouts in drain_one_turn_streaming. The watchdog runs from
# drain entry (NOT from first_chunk) so the no-response-at-all path is
# also covered. Two thresholds because pre-first-chunk silence is
# normal-ish (network + Gemini first-token latency), while post-first-chunk
# silence is a hang. Both are observability-only — drain.timeout fires
# at most once per turn and does not abort the drain.
DRAIN_WATCHDOG_TIMEOUT_S = 5.0          # silence between recv events, after first_chunk
DRAIN_FIRST_CHUNK_TIMEOUT_S = 10.0      # silence from drain start, before first_chunk
# Second-stage: if silence persists this long PAST a drain.timeout (i.e.
# >= DRAIN_WATCHDOG_TIMEOUT_S + DRAIN_HARD_ABORT_S of total silence) with
# zero recv events in between, force-abort the turn — but keep the
# Gemini Live session alive for the next turn. The session is reused
# across turns; killing it on one stuck turn would force a 5–8 s re-init.
# Run-2 turn-4 recovered 1.0 s after its drain.timeout, so 15 s of
# headroom past timeout is well above the observed recovery window.
DRAIN_HARD_ABORT_S = 15.0

# --- Streaming resampler ---
# Gemini emits 24 kHz int16 mono; robot wants 16 kHz float32 mono.
# Ratio 24:16 = 3:2 → resample_poly(up=2, down=3). To keep clean 2:3 batches
# and bounded edge artifacts, we accumulate ≥1536 24k samples (64 ms) before
# resampling, taking multiples of 3 each pass; trailing partial buffer is
# flushed through the resampler at turn end.
RESAMPLE_BATCH_24K = 1536        # 64 ms @ 24 kHz, multiple of 3 → 1024 @ 16 kHz
RESAMPLE_MIN_24K = 1536          # only resample once we have at least this much

# Local files — each conversation gets its own conversations/<timestamp>/
# folder, created by ConversationDir.
SCRIPT_DIR = Path(__file__).parent
CONVERSATIONS_ROOT = SCRIPT_DIR / "conversations"
HF_TOKEN_PATH = SCRIPT_DIR / "hf_token.txt"


def _populate_motion_catalog():
    """Return (emotions, dances) as lists of (name, description) tuples.

    Pulled at import time so the Gemini tool schemas can include the per-move
    descriptions inline. Token resolution is best-effort; if the HF token or
    libraries are missing the lists are empty and the tools degrade to no-ops.
    """
    if HF_TOKEN_PATH.exists():
        tok = HF_TOKEN_PATH.read_text(encoding="utf-8").strip()
        if tok:
            os.environ.setdefault("HF_TOKEN", tok)
            os.environ.setdefault("HUGGING_FACE_HUB_TOKEN", tok)
    emotions: list[tuple[str, str]] = []
    dances: list[tuple[str, str]] = []
    try:
        from reachy_mini.motion.recorded_move import RecordedMoves
        em = RecordedMoves("pollen-robotics/reachy-mini-emotions-library")
        for name in em.list_moves():
            try:
                desc = em.get(name).description or ""
            except Exception:
                desc = ""
            emotions.append((name, desc))
    except Exception:
        pass
    try:
        from reachy_mini_dances_library.collection.dance import AVAILABLE_MOVES
        for name, value in AVAILABLE_MOVES.items():
            metadata = value[2] if len(value) >= 3 else {}
            desc = metadata.get("description", "") if isinstance(metadata, dict) else ""
            dances.append((name, desc))
    except Exception:
        pass
    return emotions, dances


EMOTIONS_CATALOG, DANCES_CATALOG = _populate_motion_catalog()
EMOTION_NAMES = sorted(n for n, _ in EMOTIONS_CATALOG)
DANCE_NAMES = sorted(n for n, _ in DANCES_CATALOG)
HEAD_DIRECTIONS = ["left", "right", "up", "down", "front"]

# Frame protocol — see robot_streaming_player.py for full table
_HDR = struct.Struct(">I")
_TURN_END_FRAME = _HDR.pack(0x00000000)
_CLEAR_FRAME = _HDR.pack(0xFFFFFFFF)
_LISTENING_START_FRAME = _HDR.pack(0xFFFFFFFE)
_LISTENING_END_FRAME = _HDR.pack(0xFFFFFFFD)
_MOTION_HEADER = _HDR.pack(0xFFFFFFFC)  # Phase 3B; followed by [4-byte payload length][JSON]

# Component loggers. Handlers are attached to the parent "reachy" logger by
# logging_setup (init_base_logging at startup, open_conversation_log per
# conversation). Names unchanged from the monolith so downstream greps and
# the logging_setup docstring stay accurate.
log_main = logging.getLogger("reachy.main")
log_capture = logging.getLogger("reachy.audio.capture")
log_vad = logging.getLogger("reachy.audio.vad")
log_session = logging.getLogger("reachy.gemini.session")
log_stream = logging.getLogger("reachy.gemini.stream")
log_ssh = logging.getLogger("reachy.transport.ssh")
log_framing = logging.getLogger("reachy.transport.framing")
log_robot_stderr = logging.getLogger("reachy.robot.stderr")
log_state = logging.getLogger("reachy.state")
log_motion = logging.getLogger("reachy.motion")
log_tools = logging.getLogger("reachy.tools")

# Module-level EventLogger handle. Set by Conversation.run(); accessed via
# _emit() so call sites never have to null-check or try/except (the helper
# does both). During pure idle (no conversation) this is None and _emit is a
# no-op — system-scoped events go to the system log instead, not events.jsonl.
_EV: "EventLogger | None" = None

# State-machine tracking for state.transition events (the per-TURN fsm:
# IDLE/LISTENING/CAPTURING/SENDING/RECEIVING). Distinct from the system-level
# SystemState in system.py. Records the most recent state so each transition
# has a `from` field. Unchanged from the monolith.
_STATE: str = "IDLE"

# Per-turn tool-call counter. Read by _handle_tool_call to enforce
# MAX_TOOL_CALLS_PER_TURN; read by the drain watchdog too. Reset on
# turn.start AND on user.speech.start (belt-and-suspenders — speech.start
# fires before turn.start, so the earlier reset clears stale state from
# any prior turn that didn't reach turn.end).
_TOOLS_DISPATCHED_THIS_TURN: int = 0

# Phase B: optional hook invoked by _transition() on every per-turn state
# change, so SystemManager can map the raw state to a UI substate
# (LISTENING/THINKING/SPEAKING) and broadcast it over the WebSocket. Kept as a
# module global (like _EV/_STATE) because _transition() is module-level and
# fires from both the event loop (SENDING/RECEIVING) and the mic worker thread
# (LISTENING/CAPTURING); the hook must therefore be thread-safe (SystemManager
# wires it to broadcast_threadsafe). None while idle / between conversations.
_SUBSTATE_HOOK: "Callable[[str], None] | None" = None


def set_substate_hook(fn: "Callable[[str], None] | None") -> None:
    global _SUBSTATE_HOOK
    _SUBSTATE_HOOK = fn


def _reset_tool_counter() -> None:
    global _TOOLS_DISPATCHED_THIS_TURN
    _TOOLS_DISPATCHED_THIS_TURN = 0


def _bump_tool_counter() -> None:
    global _TOOLS_DISPATCHED_THIS_TURN
    _TOOLS_DISPATCHED_THIS_TURN += 1


def _emit(event: str, **fields) -> None:
    """Fire-and-forget event log. Never raises into the caller."""
    if _EV is None:
        return
    try:
        _EV.log_event(event, **fields)
    except Exception:
        log_main.exception("event log failed for %s", event)


def _ev_set_turn(turn_id: int) -> None:
    if _EV is None:
        return
    try:
        _EV.set_turn(turn_id)
    except Exception:
        log_main.exception("EventLogger.set_turn failed")


def _ev_clear_turn() -> None:
    if _EV is None:
        return
    try:
        _EV.clear_turn()
    except Exception:
        log_main.exception("EventLogger.clear_turn failed")


def _transition(to: str, reason: str = "") -> None:
    """Record a state.transition event. Emits regardless of whether the new
    state differs from the old — the spec wants `from`/`to`/`reason`."""
    global _STATE
    frm = _STATE
    _STATE = to
    log_state.debug("transition %s -> %s (%s)", frm, to, reason)
    _emit("state.transition", **{"from": frm, "to": to, "reason": reason})
    # Phase B: forward the raw state to the UI-substate hook (if a conversation
    # set one). The hook does the mapping + threadsafe WS broadcast; we pass the
    # raw state and never raise into the caller.
    hook = _SUBSTATE_HOOK
    if hook is not None:
        try:
            hook(to)
        except Exception:
            log_state.exception("substate hook failed for %s", to)


_SENTINEL_NAMES = {
    0x00000000: "TURN_END",
    0xFFFFFFFF: "CLEAR",
    0xFFFFFFFE: "LISTENING_START",
    0xFFFFFFFD: "LISTENING_END",
    0xFFFFFFFC: "MOTION",
}


# === API key resolution ============================================

def get_api_key() -> str:
    """env var, then local .gemini_key, then Tomer's known path."""
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key.strip()
    local_key = SCRIPT_DIR / ".gemini_key"
    if local_key.exists():
        return local_key.read_text().strip()
    tomer_path = Path(
        r"C:\Users\tomer\Desktop\job\reachy-mini\reachy-mini llm gemini token.txt"
    )
    if tomer_path.exists():
        return tomer_path.read_text().strip()
    sys.exit(
        "No Gemini API key found. Set GEMINI_API_KEY env var, "
        f"or create {local_key}"
    )


# === VAD-based mic capture =========================================

def record_with_vad(vad: "SileroVAD",
                    robot: "StreamingRobotPlayer | None" = None,
                    should_stop: "threading.Event | None" = None) -> np.ndarray:
    """Record from default mic using Silero-VAD endpointing.

    Per-frame: feed 32 ms (512-sample) int16 chunks into Silero, get a
    speech probability, threshold-compare. Outer-loop logic for hangover,
    max-turn, wait-for-speech, min-speech is unchanged.

    If `robot` is given, the laptop fires the listening-start sentinel the
    moment VAD first hears speech, and the listening-end sentinel when the
    recording ends (whether normally or via timeout). This lets the robot
    distinguish "user is talking" from "Gemini is talking" without
    changing the audio protocol.

    `should_stop` (Phase A): an optional threading.Event the loop checks once
    per outer iteration (~every 0.2 s). When set, capture returns an empty
    array immediately so a long mic wait doesn't delay an End-Conversation
    click. Runs on a worker thread (asyncio.to_thread), so it must not block
    the event loop and must be promptly interruptible.
    """
    # Clear the model's LSTM state — otherwise the tail of the prior
    # turn (or the bot's own audio that leaked into the mic) can bleed
    # into the first frames of this turn.
    vad.reset()

    frame_samples = SILERO_FRAME_SIZE
    frame_bytes = frame_samples * 2  # int16 PCM

    audio_frames: list[bytes] = []
    last_speech_t: float | None = None
    started = time.perf_counter()

    import queue
    q: "queue.Queue[bytes]" = queue.Queue()

    def cb(indata, frames, time_info, status):
        if status:
            log_capture.debug("sd status: %s", status)
        if indata.ndim > 1:
            indata = indata[:, 0]
        q.put(indata.tobytes())

    log_capture.info("  >>> מקשיב (דבר עכשיו) / Listening (speak now)…")
    _transition("LISTENING", reason="mic_open")
    speech_started = False
    speech_start_perf: float | None = None
    trigger_speech_prob: float = 0.0

    # Per-turn aggregates for vad.turn_summary and per-batch counters
    # for vad.frame (flushed every 20 frames = ~640 ms).
    frames_total = 0
    speech_frames_total = 0
    max_prob_total = 0.0
    sum_prob_total = 0.0
    batch_frames = 0
    batch_speech = 0
    batch_max = 0.0
    batch_sum = 0.0

    def _flush_vad_batch() -> None:
        nonlocal batch_frames, batch_speech, batch_max, batch_sum
        if batch_frames > 0:
            _emit("vad.frame",
                  frames_in_batch=batch_frames,
                  speech_frames=batch_speech,
                  max_prob=round(batch_max, 4),
                  mean_prob=round(batch_sum / batch_frames, 4))
            batch_frames = 0
            batch_speech = 0
            batch_max = 0.0
            batch_sum = 0.0

    with sd.InputStream(
        samplerate=GEMINI_INPUT_RATE,
        channels=1,
        dtype="int16",
        blocksize=SILERO_FRAME_SIZE,
        callback=cb,
    ):
        while True:
            # Phase A: bail out promptly when asked to stop (End clicked /
            # system shutting down). Checked before each blocking get so a
            # 15 s wait-for-speech doesn't hold up teardown.
            if should_stop is not None and should_stop.is_set():
                log_capture.info("  >>> capture interrupted (stop requested)")
                _flush_vad_batch()
                return np.array([], dtype=np.int16)
            try:
                raw = q.get(timeout=0.2)
            except queue.Empty:
                if (not speech_started
                        and time.perf_counter() - started > WAIT_FOR_SPEECH_S):
                    log_vad.debug("(no speech heard, going around)")
                    _flush_vad_batch()
                    return np.array([], dtype=np.int16)
                continue

            for off in range(0, len(raw) - frame_bytes + 1, frame_bytes):
                chunk = raw[off:off + frame_bytes]
                audio_frames.append(chunk)
                samples = np.frombuffer(chunk, dtype=np.int16)
                try:
                    prob = vad.feed_frame(samples)
                except Exception:
                    prob = 0.0
                is_speech = vad.is_speech(prob)

                # Aggregates
                frames_total += 1
                sum_prob_total += prob
                if prob > max_prob_total:
                    max_prob_total = prob
                if is_speech:
                    speech_frames_total += 1
                batch_frames += 1
                batch_sum += prob
                if prob > batch_max:
                    batch_max = prob
                if is_speech:
                    batch_speech += 1
                if batch_frames >= 20:
                    _flush_vad_batch()

                now = time.perf_counter()
                if is_speech:
                    last_speech_t = now
                    if not speech_started:
                        speech_started = True
                        speech_start_perf = now
                        trigger_speech_prob = prob
                        log_vad.info("  >>> שומע אותך / Hearing you.  (prob=%.3f)", prob)
                        _emit("user.speech.start",
                              trigger_speech_prob=round(prob, 4))
                        # Belt-and-suspenders: clear the per-turn tool
                        # counter as soon as the user starts speaking, in
                        # case a prior turn died before turn.end fired.
                        _reset_tool_counter()
                        _transition("CAPTURING", reason="vad_first_speech")
                        if robot is not None:
                            robot.signal_listening_start()

            now = time.perf_counter()
            if not speech_started:
                if now - started > WAIT_FOR_SPEECH_S:
                    log_vad.debug("(no speech heard, going around)")
                    _flush_vad_batch()
                    return np.array([], dtype=np.int16)
                continue
            if last_speech_t and (now - last_speech_t) >= SILENCE_HANGOVER_S:
                break
            if now - started >= MAX_TURN_S:
                log_vad.info("Hit %ds max turn duration", MAX_TURN_S)
                break

    _flush_vad_batch()

    if robot is not None and speech_started:
        robot.signal_listening_end()

    if not audio_frames:
        return np.array([], dtype=np.int16)

    audio = np.frombuffer(b"".join(audio_frames), dtype=np.int16)
    duration = len(audio) / GEMINI_INPUT_RATE
    if duration < MIN_SPEECH_S:
        log_vad.info("Turn too short (%.2fs). Skipping.", duration)
        return np.array([], dtype=np.int16)
    log_vad.info("Recorded %.2fs", duration)
    speech_end_perf = time.perf_counter()
    duration_ms = int(duration * 1000)
    _emit("user.speech.end", duration_ms=duration_ms)
    # Companion aggregate event — useful for post-hoc threshold tuning.
    if frames_total > 0:
        _emit("vad.turn_summary",
              frames_total=frames_total,
              speech_frames=speech_frames_total,
              max_prob=round(max_prob_total, 4),
              mean_prob=round(sum_prob_total / frames_total, 4),
              threshold=vad.threshold)
    # Stash the speech-end timestamp on the array so the turn loop can use it
    # for latency_ms_from_user_speech_end. Numpy ndarrays don't accept
    # arbitrary attributes; piggyback via a module-level slot keyed by id.
    _RECORD_META[id(audio)] = (speech_end_perf, speech_start_perf or speech_end_perf)
    return audio


# Side-channel for record_with_vad → turn loop to pass speech-end perf_counter
# without changing the function signature.
_RECORD_META: "dict[int, tuple[float, float]]" = {}


# === Streaming robot connection ====================================

# Robot stderr lines are forwarded verbatim, but we re-emit them on the laptop
# side at their embedded level so DEBUG-level robot chatter (notably the
# per-frame player.frame_recv flood) reaches the DEBUG file handlers
# (laptop.log, system log) yet is dropped by the INFO-level console and
# WebSocket handlers — keeping the UI event log and Copy Logs readable.
# Robot line format: "YYYY-MM-DD HH:MM:SS,mmm mono=<int> <LEVEL> <logger> <msg>".
# Lines that don't match (raw banners / non-logging stderr) fall through to INFO.
_ROBOT_LOG_LINE = re.compile(
    r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} mono=\d+ '
    r'(DEBUG|INFO|WARNING|ERROR|CRITICAL) '
)


class StreamingRobotPlayer:
    """One SSH channel to a long-running robot_streaming_player.py process.

    Audio chunks (Gemini int16 @ 24 kHz mono) are accumulated, resampled to
    16 kHz float32 mono in 64-ms batches, and pushed as length-prefixed
    frames over the channel's stdin.

    Owned by SystemManager for the whole process lifetime — constructed once
    at startup (blocking; run via asyncio.to_thread) and reused across
    conversations. The robot keeps breathing whenever no audio is queued.
    """

    def __init__(self, host: str, user: str, password: str,
                 robot_log_path: "Path | None" = None):
        log_ssh.info("Connecting to robot %s as %s…", host, user)
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(host, username=user, password=password, timeout=15)

        transport = self.client.get_transport()
        if transport is None:
            raise RuntimeError("paramiko transport is None after connect")
        self.channel = transport.open_session()
        # -u: unbuffered stdout/stderr — needed for prompt 'ready' detection.
        self.channel.exec_command(f"{ROBOT_PYTHON} -u {ROBOT_STREAMING_PLAYER}")

        self._ready = threading.Event()
        self._exited = threading.Event()
        self._stderr_lines: list[str] = []
        # Optional tee of every raw stderr line into a robot.log file. In the
        # dashboard this is left None: robot stderr already flows through the
        # log_robot_stderr logger to the "reachy" handlers (system log when
        # idle, mirrored to laptop.log during a conversation), which is the
        # routing the plan asks for. Kept for the archived CLI path.
        self._robot_log_path = robot_log_path
        self._robot_log_fh = None
        if robot_log_path is not None:
            try:
                # line-buffered (buffering=1) so a crash doesn't lose the tail
                self._robot_log_fh = open(robot_log_path, "a",
                                          encoding="utf-8", buffering=1)
            except Exception:
                log_ssh.exception("Failed to open robot.log tee at %s", robot_log_path)
                self._robot_log_fh = None
        self._drainer = threading.Thread(
            target=self._drain_stderr, name="robot-stderr", daemon=True
        )
        self._drainer.start()

        log_ssh.info("Waiting up to %ds for robot to say 'ready'…", ROBOT_READY_TIMEOUT_S)
        t0 = time.perf_counter()
        if not self._ready.wait(timeout=ROBOT_READY_TIMEOUT_S):
            self._exited.set()
            self.close()
            raise RuntimeError(
                f"robot_streaming_player.py never said 'ready' in "
                f"{ROBOT_READY_TIMEOUT_S}s. stderr so far: {self._stderr_lines}"
            )
        self.init_time_s = time.perf_counter() - t0
        log_ssh.info("Robot ready in %.2fs.", self.init_time_s)
        _emit("main.robot_ready", init_time_s=self.init_time_s)

        # Per-turn resampler state (24 kHz int16 carryover)
        self._carry_24k = np.array([], dtype=np.int16)

    @property
    def connected(self) -> bool:
        """Best-effort liveness for the status panel (Phase A): the SSH
        channel is open and the robot process hasn't signalled exit. A real
        daemon-status probe is deferred to Phase B."""
        try:
            if self._exited.is_set():
                return False
            return not self.channel.closed
        except Exception:
            return False

    # ----- stderr drainer thread -----

    def _drain_stderr(self) -> None:
        buf = b""
        ch = self.channel
        while True:
            if ch.recv_stderr_ready():
                chunk = ch.recv_stderr(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    s = line.decode("utf-8", errors="replace").rstrip()
                    if not s:
                        continue
                    self._stderr_lines.append(s)
                    # Re-emit at the robot-side level so DEBUG lines are kept
                    # out of the INFO console / WS log but still land in the
                    # DEBUG file handlers. Non-matching lines default to INFO.
                    _m = _ROBOT_LOG_LINE.match(s)
                    if _m is not None and _m.group(1) == "DEBUG":
                        log_robot_stderr.debug("%s", s)
                    else:
                        log_robot_stderr.info("%s", s)
                    if self._robot_log_fh is not None:
                        try:
                            self._robot_log_fh.write(s + "\n")
                        except Exception:
                            # Swallow per-line tee failures; do not let an I/O
                            # blip kill the stderr drainer.
                            pass
                    # The robot now emits formatted lines like
                    # "<ts> mono=… INFO  reachy.robot.player    ready".
                    # Old "bare-word" lines also need to match (in case an
                    # older robot script is still installed during rollout).
                    if s == "ready" or s.endswith(" ready"):
                        self._ready.set()
                    if s == "exit" or s.endswith(" exit"):
                        self._exited.set()
            elif ch.exit_status_ready():
                tail = ch.recv_stderr(65536)
                if tail:
                    buf += tail
                    continue
                break
            else:
                time.sleep(0.02)

    # ----- streaming API -----

    def stream_chunk(self, samples_int16_24k: np.ndarray) -> bool:
        """Accept one Gemini audio chunk (int16 mono @ 24 kHz). Resample as
        much as we can in 1536-sample batches and ship; leftovers carry over.

        Returns True iff at least one frame was sent to the robot on this call.
        """
        if samples_int16_24k.size == 0:
            return False
        self._carry_24k = (
            np.concatenate([self._carry_24k, samples_int16_24k])
            if self._carry_24k.size else samples_int16_24k
        )
        sent = False
        while self._carry_24k.size >= RESAMPLE_MIN_24K:
            batch = self._carry_24k[:RESAMPLE_BATCH_24K]
            self._carry_24k = self._carry_24k[RESAMPLE_BATCH_24K:]
            self._resample_and_send(batch)
            sent = True
        return sent

    def end_turn(self) -> None:
        """Flush any carry through the resampler, then mark end-of-turn."""
        if self._carry_24k.size:
            self._resample_and_send(self._carry_24k)
            self._carry_24k = np.array([], dtype=np.int16)
        self.channel.send(_TURN_END_FRAME)
        _emit("transport.sentinel.sent",
              sentinel_name="TURN_END", sentinel_hex="0x00000000")

    def clear_playback(self) -> None:
        """Tell the robot to drop any queued audio (mid-turn interrupt)."""
        self._carry_24k = np.array([], dtype=np.int16)
        self.channel.send(_CLEAR_FRAME)
        _emit("transport.sentinel.sent",
              sentinel_name="CLEAR", sentinel_hex="0xFFFFFFFF")

    def signal_listening_start(self) -> None:
        """Tell the robot the user has started speaking (best-effort)."""
        try:
            self.channel.send(_LISTENING_START_FRAME)
            _emit("transport.sentinel.sent",
                  sentinel_name="LISTENING_START", sentinel_hex="0xFFFFFFFE")
        except Exception:
            pass

    def signal_listening_end(self) -> None:
        """Tell the robot the user has stopped speaking (best-effort)."""
        try:
            self.channel.send(_LISTENING_END_FRAME)
            _emit("transport.sentinel.sent",
                  sentinel_name="LISTENING_END", sentinel_hex="0xFFFFFFFD")
        except Exception:
            pass

    def send_motion_command(self, cmd: dict) -> None:
        """Send a Phase 3B motion command. Frame format:

            [4-byte uint32 0xFFFFFFFC][4-byte uint32 payload_len][UTF-8 JSON]
        """
        import json as _json
        payload = _json.dumps(cmd, ensure_ascii=False).encode("utf-8")
        msg = _MOTION_HEADER + _HDR.pack(len(payload)) + payload
        try:
            self.channel.send(msg)
            _emit("transport.sentinel.sent",
                  sentinel_name="MOTION", sentinel_hex="0xFFFFFFFC",
                  payload_bytes=len(payload))
        except Exception as e:
            log_framing.error("send_motion_command failed: %s", e)

    def close(self) -> None:
        try:
            if not self.channel.closed:
                # Flush any pending carry as a final partial batch (in case
                # the caller forgot to end_turn the last turn — harmless).
                if self._carry_24k.size:
                    self._resample_and_send(self._carry_24k)
                    self._carry_24k = np.array([], dtype=np.int16)
                self.channel.shutdown_write()
        except Exception:
            pass
        try:
            rc = self.channel.recv_exit_status()
            log_ssh.info("Robot process exited rc=%d", rc)
        except Exception:
            pass
        try:
            self.channel.close()
        except Exception:
            pass
        try:
            self.client.close()
        except Exception:
            pass
        # Drain any tail-end lines the drainer thread is still flushing
        # before we close the robot.log tee fd. The drainer is a daemon
        # thread; once the channel closes it falls out of its loop.
        if self._drainer.is_alive():
            self._drainer.join(timeout=1.0)
        if self._robot_log_fh is not None:
            try:
                self._robot_log_fh.flush()
                self._robot_log_fh.close()
            except Exception:
                pass
            self._robot_log_fh = None

    # ----- internals -----

    def _resample_and_send(self, batch_24k_int16: np.ndarray) -> None:
        # int16 -> float32 in [-1, 1]
        f = batch_24k_int16.astype(np.float32) / 32768.0
        f16 = resample_poly(f, 2, 3).astype(np.float32, copy=False)
        # Clip just in case the polyphase filter overshoots
        np.clip(f16, -1.0, 1.0, out=f16)
        payload = f16.tobytes()
        self.channel.send(_HDR.pack(len(payload)) + payload)
        log_framing.debug("audio frame sent: bytes=%d samples=%d",
                          len(payload), f16.size)
        _emit("transport.audio.sent", bytes=len(payload), samples=int(f16.size))


# === Timing ========================================================

@dataclass
class TurnTimings:
    turn: int = 0
    mic_record_s: float = 0.0
    vad_to_send_s: float = 0.0
    gemini_first_chunk_s: float = 0.0
    time_to_first_audio_to_robot_s: float = 0.0
    gemini_total_s: float = 0.0
    audio_duration_s: float = 0.0
    streaming_overhead_s: float = 0.0
    wall_clock_s: float = 0.0
    user_chars: int = 0
    asst_chars: int = 0
    # Set by drain_one_turn_streaming when the second-stage watchdog
    # (drain.hard_abort) forces an early return on this turn. The turn loop
    # checks this after the await to fire turn.end with reason="gemini_silent"
    # instead of treating the return as a normal completion.
    hard_aborted: bool = False

    def log_line(self) -> str:
        return (
            f"[turn {self.turn}] mic_record={self.mic_record_s:.2f}s "
            f"vad_to_send={self.vad_to_send_s:.3f}s "
            f"gemini_first_chunk={self.gemini_first_chunk_s:.2f}s "
            f"first_to_robot={self.time_to_first_audio_to_robot_s:.2f}s "
            f"gemini_total={self.gemini_total_s:.2f}s "
            f"audio_dur={self.audio_duration_s:.2f}s "
            f"stream_overhead={self.streaming_overhead_s:+.2f}s "
            f"wall={self.wall_clock_s:.2f}s"
        )


class ConversationDir:
    """Owns the conversations/<YYYY-MM-DD_HH-MM-SS>/ folder and its files.

    (Formerly named `Conversation` in the monolith — renamed per Phase A
    adjustment A so the new turn-loop class can take the `Conversation` name.)

    transcript.txt is appended line-by-line as each turn finishes (so a
    crash or Ctrl-C still leaves a partial record). timings.csv is created
    with a header on first row and grown row-by-row. Per-turn WAVs land
    here too.
    """

    def __init__(self, root: Path = CONVERSATIONS_ROOT):
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.dir = root / timestamp
        self.dir.mkdir(parents=True, exist_ok=True)
        self.transcript = self.dir / "transcript.txt"
        self.timings = self.dir / "timings.csv"
        # Initialise empty transcript so it's visible even before turn 1
        self.transcript.touch(exist_ok=True)
        self._timings_written = False

    def turn_wav(self, turn: int) -> Path:
        return self.dir / f"turn_{turn:03d}.wav"

    def append_exchange(self, user_txt: str, asst_txt: str) -> None:
        """Append one user→robot exchange to transcript.txt immediately."""
        with self.transcript.open("a", encoding="utf-8") as f:
            f.write(f"YOU: {user_txt}\n")
            f.write(f"ROBOT: {asst_txt}\n")

    def append_timing(self, t: "TurnTimings") -> None:
        new_file = not self._timings_written and not self.timings.exists()
        with self.timings.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(asdict(t).keys()))
            if new_file:
                w.writeheader()
            w.writerow(asdict(t))
        self._timings_written = True


# === Tooling / config builders =====================================

def contains_end_phrase(text: str) -> bool:
    if not text:
        return False
    return _END_BOUNDARY_RE.search(f" {text} ") is not None


def _format_enum_doc(pairs: list[tuple[str, str]], header: str) -> str:
    """Pollen-style: cram each enum option's description into the parameter
    description so Gemini sees what to pick. Kept compact to stay below tool
    schema size limits."""
    lines = [header]
    for name, desc in pairs:
        # Single-line, trimmed, no leading/trailing punctuation noise.
        d = " ".join((desc or "").split())[:240]
        lines.append(f"- {name}: {d}" if d else f"- {name}")
    return "\n".join(lines)


def build_motion_tools() -> list:
    """Return a list of types.Tool objects, one per motion tool, populated
    from the installed-library catalog. If the libraries weren't loadable,
    each list is empty and Gemini just won't have those tools available.
    """
    tools: list = []

    if EMOTION_NAMES:
        emo_desc = _format_enum_doc(
            EMOTIONS_CATALOG,
            "Name of the emotion to play. Choose the option whose "
            "description best matches the current moment.",
        )
        play_emotion_decl = types.FunctionDeclaration(
            name="play_emotion",
            description=(
                "Play a pre-recorded emotion gesture (face/head/antennas) to express "
                "how Reachy feels in this moment. Use sparingly — not in every turn."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "name": types.Schema(
                        type=types.Type.STRING,
                        enum=list(EMOTION_NAMES),
                        description=emo_desc,
                    ),
                },
                required=["name"],
            ),
        )
        tools.append(types.Tool(function_declarations=[play_emotion_decl]))

    if DANCE_NAMES:
        dance_desc = _format_enum_doc(
            DANCES_CATALOG,
            "Name of the dance to play. Choose one whose description fits "
            "the mood; if descriptions are blank, pick by name.",
        )
        dance_decl = types.FunctionDeclaration(
            name="dance",
            description=(
                "Play a named dance move. Use only for upbeat or playful moments. "
                "Each call plays the dance once; you may set repeat (default 1, max 3)."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "name": types.Schema(
                        type=types.Type.STRING,
                        enum=list(DANCE_NAMES),
                        description=dance_desc,
                    ),
                    "repeat": types.Schema(
                        type=types.Type.INTEGER,
                        description="How many times to play in a row (default 1, max 3).",
                    ),
                },
                required=["name"],
            ),
        )
        tools.append(types.Tool(function_declarations=[dance_decl]))

    move_head_decl = types.FunctionDeclaration(
        name="move_head",
        description="Move your head to look in a direction.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "direction": types.Schema(
                    type=types.Type.STRING,
                    enum=list(HEAD_DIRECTIONS),
                    description=(
                        "Which way to look. 'front' returns to center. Use "
                        "'left'/'right' to look toward someone speaking from "
                        "that side; 'up' to look at someone tall; 'down' for "
                        "thoughtfulness."
                    ),
                ),
            },
            required=["direction"],
        ),
    )
    tools.append(types.Tool(function_declarations=[move_head_decl]))
    return tools


def build_live_config() -> "types.LiveConnectConfig":
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        system_instruction=SYSTEM_PROMPT,
        speech_config=types.SpeechConfig(
            language_code=GEMINI_LANGUAGE_CODE,
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=GEMINI_VOICE
                )
            ),
        ),
        tools=build_motion_tools() if ENABLE_TOOL_CALLS else [],
    )


def startup_flags() -> dict:
    """The flags dict re-emitted in each conversation's main.startup event so
    summary.json's `config` block stays populated (Phase A adjustment C/E)."""
    return {
        "model": GEMINI_MODEL,
        "voice": GEMINI_VOICE,
        "language": GEMINI_LANGUAGE_CODE,
        "silero_threshold": SILERO_THRESHOLD,
        "silero_frame_size": SILERO_FRAME_SIZE,
        "silence_hangover_s": SILENCE_HANGOVER_S,
        "max_turn_s": MAX_TURN_S,
        "wait_for_speech_s": WAIT_FOR_SPEECH_S,
        "min_speech_s": MIN_SPEECH_S,
        "gemini_input_rate": GEMINI_INPUT_RATE,
        "gemini_output_rate": GEMINI_OUTPUT_RATE,
        "robot_output_rate": ROBOT_OUTPUT_RATE,
        "enable_tool_calls": ENABLE_TOOL_CALLS,
        "max_tool_calls_per_turn": MAX_TOOL_CALLS_PER_TURN,
        "drain_watchdog_timeout_s": DRAIN_WATCHDOG_TIMEOUT_S,
        "drain_first_chunk_timeout_s": DRAIN_FIRST_CHUNK_TIMEOUT_S,
        "drain_hard_abort_s": DRAIN_HARD_ABORT_S,
    }


async def _handle_tool_call(tool_call, robot: "StreamingRobotPlayer", session) -> None:
    """Translate Gemini function_call events into motion-command JSON and
    ship them to the robot. Replies with a tool_response so Gemini knows
    the call completed (or what went wrong).

    Hard cap at MAX_TOOL_CALLS_PER_TURN: anything beyond the limit is
    suppressed — no MOTION sentinel sent, no FunctionResponse appended
    (Gemini gets nothing back and just keeps talking). The suppressed
    branch emits a tool.suppressed event with the function name+args.
    """
    function_calls = list(getattr(tool_call, "function_calls", []) or [])
    if not function_calls:
        return

    function_responses: list = []
    for fc in function_calls:
        name = fc.name
        args = dict(fc.args or {})

        # Per-turn hard cap. Module-level counter; reset on turn.start
        # (and again on user.speech.start as belt-and-suspenders).
        if _TOOLS_DISPATCHED_THIS_TURN >= MAX_TOOL_CALLS_PER_TURN:
            args_str = repr(args)
            if len(args_str) > 200:
                args_str = args_str[:197] + "..."
            log_tools.warning(
                "[tool] suppressed (per-turn cap=%d hit): %s(%s)",
                MAX_TOOL_CALLS_PER_TURN, name, args_str)
            _emit("tool.suppressed",
                  function_name=name,
                  function_args=args_str,
                  reason="exceeds_per_turn_limit")
            # Acknowledge the call back to Gemini with a well-formed
            # FunctionResponse — same shape as the normal-dispatch path.
            # Leaving the call un-acknowledged risks the SDK waiting on a
            # response and never emitting turn_complete (same hang class
            # as the multi-tool finding-1 bug).
            function_responses.append(types.FunctionResponse(
                id=getattr(fc, "id", None),
                name=name,
                response={"status": "suppressed",
                          "tool": name,
                          "reason": "per_turn_limit",
                          "limit": MAX_TOOL_CALLS_PER_TURN},
            ))
            continue

        status: dict = {"status": "queued", "tool": name}
        try:
            if name == "play_emotion":
                emo = args.get("name", "")
                if emo not in EMOTION_NAMES:
                    status = {"status": "error", "reason": f"unknown emotion {emo!r}"}
                    log_tools.warning("[tool] play_emotion(name=%s) -> %s", emo, status["reason"])
                else:
                    robot.send_motion_command({"type": "emotion", "name": emo})
                    log_tools.info("[tool] play_emotion(name=%s) -> queued", emo)
                    status["name"] = emo
            elif name == "dance":
                dn = args.get("name", "")
                repeat = int(args.get("repeat", 1) or 1)
                repeat = max(1, min(repeat, 3))
                if dn not in DANCE_NAMES:
                    status = {"status": "error", "reason": f"unknown dance {dn!r}"}
                    log_tools.warning("[tool] dance(name=%s) -> %s", dn, status["reason"])
                else:
                    for _ in range(repeat):
                        robot.send_motion_command({"type": "dance", "name": dn})
                    log_tools.info("[tool] dance(name=%s, repeat=%d) -> queued", dn, repeat)
                    status.update({"name": dn, "repeat": repeat})
            elif name == "move_head":
                d = args.get("direction", "")
                if d not in HEAD_DIRECTIONS:
                    status = {"status": "error", "reason": f"unknown direction {d!r}"}
                    log_tools.warning("[tool] move_head(direction=%s) -> %s", d, status["reason"])
                else:
                    robot.send_motion_command({"type": "head", "direction": d})
                    log_tools.info("[tool] move_head(direction=%s) -> queued", d)
                    status["direction"] = d
            else:
                status = {"status": "error", "reason": f"unknown tool {name!r}"}
                log_tools.warning("[tool] unknown function call: %s(%s)", name, args)
        except Exception as e:
            status = {"status": "error", "reason": f"dispatch failed: {e}"}
            log_tools.exception("[tool] dispatch failure for %s", name)

        # Count even "error" dispatches against the per-turn cap — they
        # still consumed a Gemini tool turn. Only suppressed calls don't
        # bump the counter (they were never dispatched).
        _bump_tool_counter()

        function_responses.append(types.FunctionResponse(
            id=getattr(fc, "id", None),
            name=name,
            response=status,
        ))

    if function_responses:
        try:
            await session.send_tool_response(function_responses=function_responses)
        except Exception as e:
            log_tools.warning("send_tool_response failed: %s", e)


async def drain_one_turn_streaming(
    session,
    robot: StreamingRobotPlayer,
    t: TurnTimings,
    t_send_done: float,
    t_user_speech_end: float | None = None,
) -> tuple[np.ndarray, str, str]:
    """Drain one Gemini turn, streaming each audio chunk to the robot as it
    arrives. Also accumulates the full 24 kHz WAV for disk save.
    """
    audio_chunks_24k: list[np.ndarray] = []
    user_parts: list[str] = []
    asst_parts: list[str] = []
    first_chunk_t: float | None = None
    first_to_robot_t: float | None = None

    # Batch counters for gemini.recv.audio_batch — flush every 10 chunks
    # (or at turn end). Per-chunk events would multiply the JSONL volume by
    # ~50x for no extra signal.
    recv_batch_chunks = 0
    recv_batch_samples = 0

    # Drain watchdog state. Updated on every gemini.recv.* emission; read
    # by the watchdog coroutine started at drain entry. Single mutable
    # dict so the coroutine sees updates without closures fighting over
    # `nonlocal`. Counts are cumulative-per-turn (NOT per-batch).
    drain_state = {
        "t_drain_start": time.perf_counter(),   # used pre-first-chunk
        "t_last_recv": None,                    # set when any gemini.recv.* fires
        "last_event_kind": "none",              # str; "none" until first recv event
        "last_event_t_mono_ms": 0,              # int (parallels EventLogger's t_mono_ms)
        "audio_chunks": 0,                      # cumulative per turn
        "text_parts": 0,                        # cumulative per turn
        "fired": False,                         # drain.timeout already emitted this turn
        "timeout_at_perf": None,                # perf_counter at drain.timeout emission
        "timeout_last_recv_at_perf": None,      # t_last_recv snapshot when timeout fired
        "hard_aborted": False,                  # drain.hard_abort emitted; outer should treat turn as aborted
    }

    def _mark_recv(kind: str) -> None:
        drain_state["t_last_recv"] = time.perf_counter()
        drain_state["last_event_kind"] = kind
        drain_state["last_event_t_mono_ms"] = int(time.monotonic() * 1000)

    # Capture parent task so the watchdog can cancel it on hard_abort.
    # The cancellation bubbles into the async-for loop as CancelledError,
    # which the wrapping try/except below distinguishes from a real
    # Ctrl-C by inspecting drain_state["hard_aborted"].
    parent_task = asyncio.current_task()

    async def _watchdog() -> None:
        # Runs from drain entry (NOT from first_chunk) so the
        # never-got-a-response path also fires drain.timeout. Two
        # thresholds for the first stage:
        #   - pre-first-chunk silence: from drain start, DRAIN_FIRST_CHUNK_TIMEOUT_S
        #   - post-first-chunk silence: from last recv, DRAIN_WATCHDOG_TIMEOUT_S
        # Second stage (drain.hard_abort) only arms after drain.timeout
        # has fired AND no fresh recv has updated t_last_recv since.
        try:
            while True:
                await asyncio.sleep(1.0)
                now = time.perf_counter()

                # ----- Second-stage (hard_abort) -----
                if drain_state["fired"] and not drain_state["hard_aborted"]:
                    snap = drain_state["timeout_last_recv_at_perf"]
                    if drain_state["t_last_recv"] != snap:
                        # A recv event arrived since drain.timeout — Gemini
                        # un-stuck. Disarm the second stage by clearing
                        # 'fired'; the first-stage logic can rearm next time.
                        drain_state["fired"] = False
                        drain_state["timeout_at_perf"] = None
                        drain_state["timeout_last_recv_at_perf"] = None
                        continue
                    silent_total = now - (snap if snap is not None else drain_state["t_drain_start"])
                    if (now - drain_state["timeout_at_perf"]) > DRAIN_HARD_ABORT_S:
                        _emit("drain.hard_abort",
                              seconds_silent=round(silent_total, 1),
                              seconds_since_timeout=round(now - drain_state["timeout_at_perf"], 1),
                              last_event_kind=drain_state["last_event_kind"],
                              last_event_t_mono_ms=drain_state["last_event_t_mono_ms"],
                              audio_chunks_received_this_turn=drain_state["audio_chunks"],
                              text_parts_received_this_turn=drain_state["text_parts"],
                              tool_calls_dispatched_this_turn=_TOOLS_DISPATCHED_THIS_TURN)
                        log_stream.warning(
                            "Gemini hard-abort: %.1fs total silence (turn=%s, "
                            "tools=%d, last=%s) — aborting turn, keeping session",
                            silent_total, t.turn, _TOOLS_DISPATCHED_THIS_TURN,
                            drain_state["last_event_kind"])
                        drain_state["hard_aborted"] = True
                        # Cancel the parent task to break it out of
                        # session.receive() without closing the session.
                        if parent_task is not None and not parent_task.done():
                            parent_task.cancel()
                        return
                    continue

                # ----- First-stage (drain.timeout) -----
                if drain_state["fired"]:
                    continue
                last = drain_state["t_last_recv"]
                if last is None:
                    silent = now - drain_state["t_drain_start"]
                    threshold = DRAIN_FIRST_CHUNK_TIMEOUT_S
                else:
                    silent = now - last
                    threshold = DRAIN_WATCHDOG_TIMEOUT_S
                if silent > threshold:
                    last_kind = drain_state["last_event_kind"]
                    _emit("drain.timeout",
                          seconds_silent=round(silent, 1),
                          last_event_kind=last_kind,
                          last_event_t_mono_ms=drain_state["last_event_t_mono_ms"],
                          audio_chunks_received_this_turn=drain_state["audio_chunks"],
                          text_parts_received_this_turn=drain_state["text_parts"],
                          tool_calls_dispatched_this_turn=_TOOLS_DISPATCHED_THIS_TURN)
                    # Follow-up 1: surface the silence on the console too.
                    log_stream.warning(
                        "Gemini silent for %.1fs (last=%s, tools=%d, turn=%s)",
                        silent, last_kind, _TOOLS_DISPATCHED_THIS_TURN, t.turn)
                    drain_state["fired"] = True
                    drain_state["timeout_at_perf"] = now
                    drain_state["timeout_last_recv_at_perf"] = drain_state["t_last_recv"]
        except asyncio.CancelledError:
            # Normal shutdown path — drain returned, finally cancels us.
            return

    def _flush_recv_batch() -> None:
        nonlocal recv_batch_chunks, recv_batch_samples
        if recv_batch_chunks > 0:
            _emit("gemini.recv.audio_batch",
                  chunks=recv_batch_chunks, samples=recv_batch_samples)
            _mark_recv("audio_batch")
            recv_batch_chunks = 0
            recv_batch_samples = 0

    # Start the watchdog BEFORE the receive loop, so the no-response-
    # at-all path (Gemini got silence and returned nothing) also fires
    # drain.timeout. Previously the task was started inside the loop
    # after first_chunk arrived, which never happens on that failure mode.
    watchdog_task: "asyncio.Task" = asyncio.create_task(_watchdog())
    try:
        async for resp in session.receive():
            # Late-arrival guard: if the watchdog already fired
            # drain.hard_abort for this turn, treat any further events as
            # stale (e.g. Gemini un-sticks 25 s late after we've moved on).
            # Log at WARNING so it's visible without bloating events.jsonl.
            if drain_state["hard_aborted"]:
                kind = "tool_call" if getattr(resp, "tool_call", None) else (
                    "server_content" if getattr(resp, "server_content", None) else "other")
                log_stream.warning(
                    "Discarding late Gemini event after hard_abort (kind=%s, turn=%s)",
                    kind, t.turn)
                continue
            # ----- Phase 3B: motion tool calls -----
            # The Live API yields tool_call events on the same async stream as
            # server_content. Handle them first; they're fire-and-forget on our
            # side (the robot acks by playing the motion).
            if getattr(resp, "tool_call", None) is not None:
                await _handle_tool_call(resp.tool_call, robot, session)
                continue
            # Some SDK versions also surface tool_call_cancellation
            if getattr(resp, "tool_call_cancellation", None) is not None:
                log_tools.info("[tool] cancellation received: %s", resp.tool_call_cancellation)
                continue

            sc = resp.server_content
            if sc is None:
                continue

            if sc.model_turn and sc.model_turn.parts:
                for part in sc.model_turn.parts:
                    if part.inline_data and part.inline_data.data:
                        b = part.inline_data.data
                        if isinstance(b, str):
                            b = base64.b64decode(b)
                        if b:
                            now = time.perf_counter()
                            if first_chunk_t is None:
                                first_chunk_t = now
                                base_t = t_user_speech_end if t_user_speech_end is not None else t_send_done
                                _emit("gemini.recv.first_chunk",
                                      latency_ms_from_user_speech_end=int((now - base_t) * 1000))
                                _mark_recv("first_chunk")
                                _transition("RECEIVING", reason="gemini_first_chunk")
                                # Watchdog is already running (started at
                                # drain entry); _mark_recv above flipped
                                # its threshold from FIRST_CHUNK_TIMEOUT_S
                                # to the tighter post-first-chunk one.
                            samples = np.frombuffer(b, dtype=np.int16)
                            audio_chunks_24k.append(samples)
                            sent = robot.stream_chunk(samples)
                            if sent and first_to_robot_t is None:
                                first_to_robot_t = time.perf_counter()
                            log_stream.debug("recv chunk: samples=%d", samples.size)
                            recv_batch_chunks += 1
                            recv_batch_samples += int(samples.size)
                            drain_state["audio_chunks"] += 1
                            if recv_batch_chunks >= 10:
                                _flush_recv_batch()

            if sc.input_transcription and sc.input_transcription.text:
                txt = sc.input_transcription.text
                user_parts.append(txt)
                _emit("gemini.recv.text", kind="user", text=txt)
                _mark_recv("text")
                drain_state["text_parts"] += 1
            if sc.output_transcription and sc.output_transcription.text:
                txt = sc.output_transcription.text
                asst_parts.append(txt)
                _emit("gemini.recv.text", kind="assistant", text=txt)
                _mark_recv("text")
                drain_state["text_parts"] += 1

            if sc.turn_complete:
                _emit("gemini.recv.end_of_turn")
                _mark_recv("end_of_turn")
                break
        _flush_recv_batch()
    except asyncio.CancelledError:
        # Two distinct sources raise this here:
        #   (a) the watchdog cancelled us via parent_task.cancel() after
        #       drain.hard_abort — swallow, fall through to return the
        #       partial buffer, and let the caller emit turn.end(gemini_silent).
        #   (b) the conversation was cancelled (system shutdown) — re-raise so
        #       the run() CancelledError arm handles it (turn.end cancelled,
        #       session.close cancelled, main.shutdown cancelled).
        if drain_state["hard_aborted"]:
            t.hard_aborted = True
            _flush_recv_batch()
        else:
            raise
    finally:
        # Always cancel the watchdog on drain exit — normal break, normal
        # cancel, AND hard_abort all converge here. asyncio task leaks
        # are silent killers under repeated cancellation.
        if watchdog_task is not None:
            watchdog_task.cancel()
            try:
                await watchdog_task
            except (asyncio.CancelledError, Exception):
                pass

    # Flush trailing partial-batch and emit turn-end marker
    robot.end_turn()
    if first_to_robot_t is None and audio_chunks_24k:
        # Audio arrived but resampler buffer never reached the threshold,
        # so the only frame sent was at end_turn(). Use that timestamp.
        first_to_robot_t = time.perf_counter()

    t_done = time.perf_counter()
    t.gemini_first_chunk_s = (
        (first_chunk_t - t_send_done) if first_chunk_t is not None else 0.0
    )
    t.time_to_first_audio_to_robot_s = (
        (first_to_robot_t - t_send_done) if first_to_robot_t is not None else 0.0
    )
    t.gemini_total_s = t_done - t_send_done

    response_audio = (
        np.concatenate(audio_chunks_24k) if audio_chunks_24k
        else np.array([], dtype=np.int16)
    )
    t.audio_duration_s = len(response_audio) / GEMINI_OUTPUT_RATE if response_audio.size else 0.0
    # Positive ⇒ Gemini delivered slower than realtime (robot will underrun).
    # Negative ⇒ we got audio faster than realtime (robot keeps up).
    t.streaming_overhead_s = t.gemini_total_s - t.audio_duration_s

    return response_audio, "".join(user_parts).strip(), "".join(asst_parts).strip()


# === Conversation (turn loop) ======================================

class Conversation:
    """One started conversation: owns the Gemini Live session and the turn
    loop. Ephemeral — created by SystemManager.start_conversation and discarded
    when it ends. The VAD, the robot connection, and the genai client are owned
    by SystemManager and passed in (reused across conversations).

    run() is the old laptop_chat.main() body, adapted to:
      - reuse the injected VAD / robot / client instead of constructing them,
      - honor an external stop_event (End button / shutdown) alongside the
        existing end-phrase check,
      - run the blocking mic capture off the event loop with a stop mirror,
      - own the module-level _EV for its lifetime,
      - NOT close the robot (SystemManager owns it),
      - push transcript lines to an injected callback for the dashboard.

    The reopen-on-hard-abort outer session loop is preserved verbatim
    (Phase A adjustment F).
    """

    def __init__(
        self,
        client: "genai.Client",
        vad: "SileroVAD",
        robot: "StreamingRobotPlayer",
        convo_dir: "ConversationDir | None" = None,
        on_transcript: "Callable[[str, int, str], None] | None" = None,
        on_turn_aborted: "Callable[[int, str], None] | None" = None,
    ):
        self.client = client
        self.vad = vad
        self.robot = robot
        self.dir = convo_dir or ConversationDir()
        self.on_transcript = on_transcript
        self.on_turn_aborted = on_turn_aborted

        self.id = self.dir.dir.name
        from datetime import datetime
        self.started_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
        self.turn = 0
        self.summary: dict | None = None
        # Accumulated transcript for the WS snapshot (refreshed browser tab).
        self.transcript: list[dict] = []

        # Stop signalling: asyncio.Event for the loop, threading.Event mirror
        # for the in-flight blocking mic capture (Phase A adjustment B).
        self.stop_event = asyncio.Event()
        self._stop_flag = threading.Event()
        self._stop_reason = "user"
        self._ev: "EventLogger | None" = None

    def _push_transcript(self, role: str, turn_id: int, text: str) -> None:
        self.transcript.append({"role": role, "turn_id": turn_id, "text": text})
        if self.on_transcript is not None:
            try:
                self.on_transcript(role, turn_id, text)
            except Exception:
                log_main.exception("on_transcript callback failed")

    def _push_aborted(self, turn_id: int, reason: str) -> None:
        """Record an aborted turn (no transcript pair) for the chat box. Stored
        in the transcript list (role "aborted") so it also survives a WS
        snapshot on reconnect, and pushed live via the on_turn_aborted hook."""
        self.transcript.append({"role": "aborted", "turn_id": turn_id, "reason": reason})
        if self.on_turn_aborted is not None:
            try:
                self.on_turn_aborted(turn_id, reason)
            except Exception:
                log_main.exception("on_turn_aborted callback failed")

    async def stop(self, reason: str = "user") -> None:
        """Signal the run loop to finish. Sets both the asyncio event (checked
        between turns) and the threading mirror (cuts a blocking mic wait
        short). Awaiting completion is the caller's job — SystemManager awaits
        the run task after calling this. `reason` is recorded as the run's
        shutdown_reason (e.g. "user" for the End button, "shutdown" for
        process teardown)."""
        self._stop_reason = reason
        self._stop_flag.set()
        self.stop_event.set()

    async def run(self) -> str:
        """Run the turn loop until end-phrase, stop_event, or fatal error.

        Returns the shutdown reason string ("end_phrase" | "user" |
        "cancelled" | "exception:<Type>"). On an unhandled exception the
        reason is recorded in telemetry and the exception is re-raised so the
        SystemManager wrapper can broadcast a crash.
        """
        global _EV, _STATE

        convo = self.dir
        _EV = EventLogger(convo.dir, conversation_id=convo.dir.name)
        self._ev = _EV
        _STATE = "IDLE"

        # Re-emit startup flags per conversation so summary.config stays
        # populated (the system-wide VAD/robot bring-up already happened and
        # logged to the system log, not here).
        _emit("main.startup",
              version_or_git_sha="phase4-dashboard",
              flags=startup_flags(),
              conversation_id=convo.dir.name)

        log_main.info("=" * 60)
        log_main.info("Conversation %s — turn loop start", convo.dir.name)
        log_main.info("Model: %s  |  Voice: %s  |  Lang: %s",
                      GEMINI_MODEL, GEMINI_VOICE, GEMINI_LANGUAGE_CODE)
        log_main.info("Saving to: %s", convo.dir.relative_to(SCRIPT_DIR))
        log_main.info("End with: 'להתראות' / 'סיים שיחה' / 'goodbye' / End button")
        log_main.info("=" * 60)

        turn = 0
        cfg = build_live_config()
        shutdown_reason = "user"
        session_attempt = 0           # 0 = initial open; incremented on each reopen
        reopen_prev_turn_id = None    # turn id that triggered the pending reopen

        try:
            while True:  # session loop — re-enters with a fresh session after a hard-abort
                if self.stop_event.is_set():
                    shutdown_reason = self._stop_reason
                    break
                reopen_after_hard_abort = False
                _t_connect_start = time.perf_counter()
                async with self.client.aio.live.connect(model=GEMINI_MODEL, config=cfg) as session:
                    _connect_ms = int((time.perf_counter() - _t_connect_start) * 1000)
                    if session_attempt == 0:
                        log_session.info("Gemini Live session opened. Reusing for all turns.")
                        _emit("gemini.session.open")
                    else:
                        log_session.info(
                            "Gemini Live session reopened (attempt %d, %dms) after hard-abort.",
                            session_attempt, _connect_ms)
                        _emit("gemini.session.reopen",
                              reason="hard_abort",
                              prev_turn_id=reopen_prev_turn_id,
                              latency_ms=_connect_ms,
                              attempt_index=session_attempt)
                    while True:
                        if self.stop_event.is_set():
                            shutdown_reason = self._stop_reason
                            break
                        turn += 1
                        self.turn = turn
                        log_main.info("--- Turn %d ---", turn)
                        t = TurnTimings(turn=turn)
                        t_turn_start = time.perf_counter()

                        t_mic_start = time.perf_counter()
                        mic_audio = await asyncio.to_thread(
                            record_with_vad, self.vad, self.robot, self._stop_flag)
                        t.mic_record_s = time.perf_counter() - t_mic_start
                        if mic_audio.size == 0:
                            # Either no viable speech, or a stop was requested
                            # mid-capture. No turn.start was emitted (we only
                            # emit it for viable turns). Reset the implicit FSM.
                            if self.stop_event.is_set():
                                shutdown_reason = self._stop_reason
                                break
                            _transition("IDLE", reason="no_speech_or_too_short")
                            continue

                        # Viable audio captured — open the turn proper.
                        _ev_set_turn(turn)
                        _emit("turn.start", turn_id=turn)
                        _reset_tool_counter()
                        meta = _RECORD_META.pop(id(mic_audio), None)
                        t_user_speech_end = meta[0] if meta else time.perf_counter()
                        _transition("SENDING", reason="vad_done")

                        t_send_start = time.perf_counter()
                        try:
                            await session.send_realtime_input(
                                audio=types.Blob(
                                    data=mic_audio.tobytes(),
                                    mime_type=f"audio/pcm;rate={GEMINI_INPUT_RATE}",
                                )
                            )
                            await session.send_realtime_input(audio_stream_end=True)
                        except Exception as e:
                            log_stream.error("Failed to send audio: %s", e)
                            _emit("gemini.session.error",
                                  error_type=type(e).__name__, message=str(e))
                            _emit("turn.end", turn_id=turn,
                                  total_ms=int((time.perf_counter() - t_turn_start) * 1000),
                                  gemini_first_chunk_ms=0,
                                  samples_sent_to_robot=0,
                                  aborted=True, reason="send_failed")
                            _ev_clear_turn()
                            _transition("IDLE", reason="send_failed")
                            continue
                        t_send_done = time.perf_counter()
                        t.vad_to_send_s = t_send_done - t_send_start
                        # The laptop sends one blob per turn rather than streaming.
                        # Report as a single-batch event so the event-log schema
                        # stays consistent with the spec.
                        _emit("gemini.send.audio_batch",
                              chunks=1,
                              samples=int(mic_audio.size),
                              duration_ms=int(mic_audio.size / GEMINI_INPUT_RATE * 1000))

                        try:
                            response_audio, user_txt, asst_txt = await drain_one_turn_streaming(
                                session, self.robot, t, t_send_done,
                                t_user_speech_end=t_user_speech_end,
                            )
                        except asyncio.CancelledError:
                            # Conversation cancelled (system shutdown) during
                            # drain. Emit turn.end with aborted=cancelled, then
                            # re-raise so the outer handler sets shutdown_reason
                            # and the finally block emits the matching
                            # session.close / main.shutdown.
                            _emit("turn.end", turn_id=turn,
                                  total_ms=int((time.perf_counter() - t_turn_start) * 1000),
                                  gemini_first_chunk_ms=0,
                                  samples_sent_to_robot=0,
                                  aborted=True, reason="cancelled")
                            _ev_clear_turn()
                            _transition("IDLE", reason="cancelled")
                            raise
                        except Exception as e:
                            log_stream.error("Gemini drain failed: %s", e)
                            _emit("gemini.session.error",
                                  error_type=type(e).__name__, message=str(e))
                            # Still send a turn-end so robot moves on
                            try:
                                self.robot.end_turn()
                            except Exception:
                                pass
                            _emit("turn.end", turn_id=turn,
                                  total_ms=int((time.perf_counter() - t_turn_start) * 1000),
                                  gemini_first_chunk_ms=0,
                                  samples_sent_to_robot=0,
                                  aborted=True, reason="drain_failed")
                            _ev_clear_turn()
                            _transition("IDLE", reason="drain_failed")
                            continue

                        # Drain returned. If the watchdog hard-aborted this turn
                        # (Gemini went silent past DRAIN_HARD_ABORT_S), emit
                        # turn.end with reason="gemini_silent", then tear down and
                        # reopen the session — it is left in a zombie state that
                        # emits no audio on subsequent turns. The partial audio
                        # buffer was already streamed to the robot inside drain.
                        if t.hard_aborted:
                            log_main.warning("Turn %d hard-aborted (Gemini silent).", turn)
                            samples_so_far = int(response_audio.size * 2 // 3) if response_audio.size else 0
                            _emit("turn.end",
                                  turn_id=turn,
                                  total_ms=int((time.perf_counter() - t_turn_start) * 1000),
                                  gemini_first_chunk_ms=int(t.gemini_first_chunk_s * 1000),
                                  samples_sent_to_robot=samples_so_far,
                                  aborted=True, reason="gemini_silent")
                            # Chat box: aborted turn produced no transcript pair
                            # (the success-path _push_transcript below is never
                            # reached). Record a placeholder so the UI shows
                            # "Turn N aborted — Gemini silent" instead of a gap.
                            self._push_aborted(turn, "gemini_silent")
                            _ev_clear_turn()
                            _transition("IDLE", reason="gemini_silent")
                            # Break the turn loop so the async-with closes this
                            # degraded session; the session loop opens a fresh one.
                            log_session.info(
                                "Closing degraded Gemini session after hard-abort on turn %d.", turn)
                            reopen_prev_turn_id = turn
                            session_attempt += 1
                            reopen_after_hard_abort = True
                            break

                        log_stream.info("  YOU:    %s", user_txt or "<no transcript>")
                        log_stream.info("  ROBOT:  %s", asst_txt or "<no transcript>")
                        # Persist this exchange now so a crash leaves a partial record.
                        convo.append_exchange(user_txt, asst_txt)
                        # Push to the dashboard chat box.
                        self._push_transcript("user", turn, user_txt)
                        self._push_transcript("robot", turn, asst_txt)
                        t.user_chars = len(user_txt)
                        t.asst_chars = len(asst_txt)

                        # Save the full-fidelity WAV (24 kHz, Gemini's native rate)
                        # alongside the streaming behavior — for offline review.
                        if response_audio.size:
                            sf.write(convo.turn_wav(turn), response_audio,
                                     GEMINI_OUTPUT_RATE, subtype="PCM_16")
                        else:
                            log_stream.warning("Gemini returned no audio for turn %d", turn)

                        t.wall_clock_s = time.perf_counter() - t_turn_start
                        log_main.info(t.log_line())
                        convo.append_timing(t)

                        # samples_sent_to_robot is reported in 16 kHz frames after
                        # resampling. response_audio is at 24 kHz; ratio 2:3.
                        samples_sent_to_robot = int(response_audio.size * 2 // 3) if response_audio.size else 0
                        _emit("turn.end",
                              turn_id=turn,
                              total_ms=int(t.wall_clock_s * 1000),
                              gemini_first_chunk_ms=int(t.gemini_first_chunk_s * 1000),
                              samples_sent_to_robot=samples_sent_to_robot)
                        _ev_clear_turn()
                        _transition("IDLE", reason="turn_complete")

                        if contains_end_phrase(user_txt):
                            log_main.info("End phrase detected. Wrapping up.")
                            shutdown_reason = "end_phrase"
                            break
                # async-with exited here: the old session is closed by __aexit__.
                if reopen_after_hard_abort:
                    continue   # session loop: open a fresh session for the next turn
                break          # normal completion (end-phrase or stop)

        except asyncio.CancelledError:
            # Conversation task cancelled (system shutdown fallback). Mirror the
            # monolith: set the reason and let finally emit closing telemetry;
            # do not re-raise (returning cleanly keeps telemetry honest).
            log_main.info("Cancelled.")
            shutdown_reason = "cancelled"
        except Exception as e:
            log_session.exception("Session-level failure")
            _emit("gemini.session.error",
                  error_type=type(e).__name__, message=str(e))
            shutdown_reason = f"exception:{type(e).__name__}"
            raise
        finally:
            # Closing telemetry FIRST so a late interruption still leaves the
            # session.close + main.shutdown records on disk. Per-conversation
            # scope: we do NOT close the robot here (SystemManager owns it and
            # keeps it breathing for the next conversation).
            _emit("gemini.session.close", reason=shutdown_reason)
            _emit("main.shutdown", reason=shutdown_reason)
            log_main.info("Conversation saved to %s",
                          convo.dir.relative_to(SCRIPT_DIR))
            # Close the event log before generating the summary so all writes
            # are flushed and the fd is released (matters on Windows).
            try:
                if _EV is not None:
                    _EV.close()
            except Exception:
                log_main.exception("EventLogger.close failed")
            # Generate the per-conversation summary.json. Failure here is
            # logged but never raised.
            try:
                from summary import generate_summary
                self.summary = generate_summary(convo.dir)
            except Exception:
                logging.getLogger("reachy.summary").exception(
                    "generate_summary failed for %s", convo.dir)
            # Relinquish the module-global event log — idle has no events.jsonl.
            _EV = None
            self._ev = None

        return shutdown_reason
