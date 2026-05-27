#!/usr/bin/env python3
"""
Reachy Mini — long-running streaming audio player WITH idle breathing,
speech-reactive head motion (Phase 3A), and LLM-driven emotion / dance /
move_head commands (Phase 3B).

One ReachyMini context for the whole conversation. Reads length-prefixed
audio chunks and motion-command JSON from stdin and plays them via the
SDK. A parallel 100 Hz motion loop evaluates the current "primary move"
(BreathingMove unless one was explicitly queued from the laptop) and adds
speech-derived secondary offsets from the SwayRollRT tapper.

Threads (in one Python process):
    StdinReader (main) — reads framed messages, decodes audio, feeds the
                         tapper, enqueues samples for playback; on motion
                         commands, swaps the current primary move.
    AudioPlayer        — pulls float32 frames from queue, calls
                         mini.media.push_audio_sample.
    MotionLoop         — runs at 100 Hz, evaluates current primary +
                         secondary offsets, calls mini.set_target.

Wire protocol on stdin (big-endian, length-prefixed):

    [4-byte uint32 length][payload of `length` bytes]

Message types — most are payload-less sentinels (length holds the type):

    length == 0x00000000             end-of-turn marker; empty payload
    length == 0xFFFFFFFF             clear playback buffer; empty payload
    length == 0xFFFFFFFE             listening-start; empty payload  (Phase 3A; logged)
    length == 0xFFFFFFFD             listening-end;   empty payload  (Phase 3A; logged)
    length == 0xFFFFFFFC             motion command (Phase 3B). UNIQUE: this sentinel
                                     IS FOLLOWED BY a second 4-byte uint32 (payload
                                     length), then `payload_length` bytes of UTF-8
                                     JSON. See _handle_motion() for the schema.
    other (1 .. 2**32-5)             audio chunk; payload = float32 PCM at the
                                     robot's output rate (16 kHz mono)

Status messages go to stderr. stdout is unused (reserved for future protocol).
EOF on stdin = end of conversation. Cleanup, then exit 0.

Phase-2 fallback: /home/pollen/scripts/robot_play.py one-shot player.
Phase-2 of this file: /home/pollen/scripts/robot_streaming_player_phase2.py.bak
Phase-3A of this file: /home/pollen/scripts/robot_streaming_player_phase3a.py.bak
"""
import math
import os
import sys
import json
import logging
import struct
import queue
import threading
import time
import traceback
from typing import Tuple, Optional

import numpy as np

# HF token must be in env BEFORE we import the recorded-move library, so
# huggingface_hub picks it up for repo authentication. The token file is
# deployed by the laptop-side _robot_prep.py to /home/pollen/.hf_token
# with chmod 600.
_TOKEN_PATH = "/home/pollen/.hf_token"
if os.path.exists(_TOKEN_PATH):
    with open(_TOKEN_PATH) as _tf:
        _hf_token = _tf.read().strip()
    if _hf_token:
        os.environ["HF_TOKEN"] = _hf_token
        os.environ["HUGGING_FACE_HUB_TOKEN"] = _hf_token
        del _hf_token  # never log the value

from reachy_mini import ReachyMini
from reachy_mini.utils import create_head_pose
from reachy_mini.utils.interpolation import (
    compose_world_offset,
    linear_pose_interpolation,
)

# Vendored DSP (verbatim port from pollen-robotics/reachy_mini_conversation_app)
from robot_speech_tapper import SwayRollRT, SR as TAPPER_SR

# Optional: emotion + dance libraries. If they aren't available we still
# accept motion commands but log an error and stay on the current move.
try:
    from reachy_mini.motion.recorded_move import RecordedMoves
    _RECORDED_MOVES_AVAILABLE = True
except Exception:
    RecordedMoves = None  # type: ignore
    _RECORDED_MOVES_AVAILABLE = False

try:
    from reachy_mini_dances_library.collection.dance import AVAILABLE_MOVES as DANCES_AVAILABLE_MOVES
    from reachy_mini_dances_library.dance_move import DanceMove
    _DANCES_AVAILABLE = True
except Exception:
    DANCES_AVAILABLE_MOVES = {}  # type: ignore
    DanceMove = None  # type: ignore
    _DANCES_AVAILABLE = False


HEADER_LEN = 4
TURN_END = 0x00000000
LISTENING_END = 0xFFFFFFFD
LISTENING_START = 0xFFFFFFFE
CLEAR_BUFFER = 0xFFFFFFFF
MOTION_COMMAND = 0xFFFFFFFC  # NEW in Phase 3B

# Motion loop
MOTION_HZ = 100.0
MOTION_PERIOD_S = 1.0 / MOTION_HZ

# move_head deltas (port from Pollen's tools/move_head.py — degrees)
HEAD_DELTAS = {
    "left":  (0, 0, 0, 0,   0,  40),
    "right": (0, 0, 0, 0,   0, -40),
    "up":    (0, 0, 0, 0, -30,   0),
    "down":  (0, 0, 0, 0,  30,   0),
    "front": (0, 0, 0, 0,   0,   0),
}
GOTO_DURATION_S = 1.0  # how long a move_head transition takes


# ---- Logging setup (named "reachy.robot.*"). stderr is the transport
# back to the laptop's drainer, which tees raw lines into
# conversation_dir/robot.log on the laptop. No JSONL on robot — the
# per-line format carries everything the summary generator needs.
# Duplicating the monotonic_ms filter class inline because importing
# from the laptop module would be a deployment headache.
class _MonotonicMsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.monotonic_ms = int(time.monotonic() * 1000)
        return True


_robot_root = logging.getLogger("reachy.robot")
_robot_root.setLevel(logging.DEBUG)
_robot_root.propagate = False
for _h in list(_robot_root.handlers):
    _robot_root.removeHandler(_h)
_robot_handler = logging.StreamHandler(stream=sys.stderr)
_robot_handler.setLevel(logging.DEBUG)
_robot_handler.setFormatter(logging.Formatter(
    fmt="%(asctime)s mono=%(monotonic_ms)d %(levelname)-5s %(name)-22s %(message)s",
))
_robot_handler.addFilter(_MonotonicMsFilter())
_robot_root.addHandler(_robot_handler)

log_player = logging.getLogger("reachy.robot.player")
log_xport = logging.getLogger("reachy.robot.transport")


def _emit_player_startup() -> None:
    """One-shot startup line. Laptop drainer keys 'ready' off another line."""
    try:
        import reachy_mini as _rm
        sdk_version = getattr(_rm, "__version__", "unknown")
    except Exception:
        sdk_version = "unknown"
    log_player.info("player.startup sdk_version=%s daemon_status=starting",
                    sdk_version)


def read_exact(stream, n: int) -> Optional[bytes]:
    """Read exactly `n` bytes from `stream`. Returns None on clean EOF."""
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


# ===================================================================== #
#  Primary moves                                                        #
# ===================================================================== #

class BreathingMove:
    """Interpolate from a starting pose to neutral, then breathe forever.

    Verbatim port of pollen-robotics/reachy_mini_conversation_app
    src/reachy_mini_conversation_app/moves.py BreathingMove.
    """

    breathing_z_amplitude = 0.005
    breathing_frequency = 0.1
    neutral_antennas = np.array([-0.1745, 0.1745])
    antenna_sway_amplitude = math.radians(15)
    antenna_frequency = 0.5

    def __init__(
        self,
        interpolation_start_pose: np.ndarray,
        interpolation_start_antennas: Tuple[float, float],
        interpolation_duration: float = 1.0,
    ):
        self.interpolation_start_pose = interpolation_start_pose
        self.interpolation_start_antennas = np.array(interpolation_start_antennas,
                                                     dtype=np.float64)
        self.interpolation_duration = float(interpolation_duration)
        self.neutral_head_pose = create_head_pose(0, 0, 0, 0, 0, 0, degrees=True)

    @property
    def duration(self) -> float:
        return float("inf")

    def evaluate(self, t: float):
        if t < self.interpolation_duration:
            r = t / self.interpolation_duration
            head_pose = linear_pose_interpolation(
                self.interpolation_start_pose, self.neutral_head_pose, r
            )
            antennas = (
                (1.0 - r) * self.interpolation_start_antennas
                + r * self.neutral_antennas
            ).astype(np.float64)
        else:
            bt = t - self.interpolation_duration
            z = self.breathing_z_amplitude * math.sin(
                2 * math.pi * self.breathing_frequency * bt
            )
            head_pose = create_head_pose(x=0, y=0, z=z, roll=0, pitch=0,
                                         yaw=0, degrees=True, mm=False)
            sway = self.antenna_sway_amplitude * math.sin(
                2 * math.pi * self.antenna_frequency * bt
            )
            antennas = np.array([sway, -sway], dtype=np.float64)
        return head_pose, antennas, 0.0


class EmotionMove:
    """Wraps a RecordedMoves entry so it conforms to our primary-move protocol."""

    def __init__(self, name: str, recorded_moves):
        self._inner = recorded_moves.get(name)
        self.name = name

    @property
    def duration(self) -> float:
        return float(self._inner.duration)

    def evaluate(self, t: float):
        head_pose, antennas, body_yaw = self._inner.evaluate(t)
        if isinstance(antennas, tuple):
            antennas = np.array([antennas[0], antennas[1]], dtype=np.float64)
        return head_pose, antennas, body_yaw if body_yaw is not None else 0.0


class DanceMoveWrapper:
    """Wraps a reachy_mini_dances_library DanceMove for our primary-move protocol."""

    def __init__(self, name: str):
        if DanceMove is None:
            raise RuntimeError("reachy_mini_dances_library not available")
        self._inner = DanceMove(name)
        self.name = name

    @property
    def duration(self) -> float:
        return float(self._inner.duration)

    def evaluate(self, t: float):
        head_pose, antennas, body_yaw = self._inner.evaluate(t)
        if isinstance(antennas, tuple):
            antennas = np.array([antennas[0], antennas[1]], dtype=np.float64)
        return head_pose, antennas, body_yaw if body_yaw is not None else 0.0


class GotoMove:
    """Linear interpolation from current pose to a fixed target pose.
    Used for move_head commands."""

    def __init__(self,
                 start_head_pose: np.ndarray,
                 target_head_pose: np.ndarray,
                 start_antennas: Tuple[float, float] = (0.0, 0.0),
                 target_antennas: Tuple[float, float] = (0.0, 0.0),
                 duration: float = GOTO_DURATION_S):
        self.start_head_pose = start_head_pose
        self.target_head_pose = target_head_pose
        self.start_antennas = np.array(start_antennas, dtype=np.float64)
        self.target_antennas = np.array(target_antennas, dtype=np.float64)
        self._duration = float(duration)

    @property
    def duration(self) -> float:
        return self._duration

    def evaluate(self, t: float):
        r = max(0.0, min(1.0, t / self._duration))
        head_pose = linear_pose_interpolation(
            self.start_head_pose, self.target_head_pose, r
        )
        antennas = (
            (1.0 - r) * self.start_antennas + r * self.target_antennas
        ).astype(np.float64)
        return head_pose, antennas, 0.0


# ===================================================================== #
#  Shared state                                                         #
# ===================================================================== #

class SharedState:
    """Cross-thread holders. The primary move is swappable by stdin
    reader; the motion loop reads it under the same lock.
    """

    def __init__(self):
        self._lock = threading.RLock()
        # (x_m, y_m, z_m, roll_rad, pitch_rad, yaw_rad) additive head offset
        self._secondary_offsets = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        self._listening = False
        # Primary move + its start time. Set by main() before threads run.
        self._primary_move = None
        self._move_start_t: float = 0.0

    def set_offsets_from_hop(self, hop: dict) -> None:
        offsets = (
            hop["x_mm"] * 0.001,
            hop["y_mm"] * 0.001,
            hop["z_mm"] * 0.001,
            hop["roll_rad"],
            hop["pitch_rad"],
            hop["yaw_rad"],
        )
        with self._lock:
            self._secondary_offsets = offsets

    def get_offsets(self):
        with self._lock:
            return self._secondary_offsets

    def clear_offsets(self) -> None:
        with self._lock:
            self._secondary_offsets = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    def set_listening(self, on: bool) -> None:
        with self._lock:
            self._listening = on

    def is_listening(self) -> bool:
        with self._lock:
            return self._listening

    def set_primary_move(self, move, start_t: Optional[float] = None) -> None:
        with self._lock:
            self._primary_move = move
            self._move_start_t = start_t if start_t is not None else time.monotonic()

    def get_primary_move(self):
        """Returns (move, t_elapsed_seconds_since_start)."""
        with self._lock:
            if self._primary_move is None:
                return None, 0.0
            return self._primary_move, time.monotonic() - self._move_start_t


# ===================================================================== #
#  Motion loop                                                          #
# ===================================================================== #

class LoopFreqStats:
    def __init__(self):
        self.count = 0
        self.mean_hz = 0.0
        self.min_hz = float("inf")

    def add_period(self, dt: float) -> None:
        if dt <= 0:
            return
        hz = 1.0 / dt
        self.count += 1
        self.mean_hz += (hz - self.mean_hz) / self.count
        if hz < self.min_hz:
            self.min_hz = hz

    def summary(self) -> str:
        if not self.count:
            return "(no ticks)"
        return (f"count={self.count} mean_hz={self.mean_hz:.2f} "
                f"min_hz={self.min_hz:.2f}")


def _make_fallback_breathing(mini: ReachyMini) -> BreathingMove:
    """Construct a fresh BreathingMove starting from the current pose."""
    try:
        start_pose = np.asarray(mini.get_current_head_pose(), dtype=np.float64)
    except Exception:
        start_pose = create_head_pose(0, 0, 0, 0, 0, 0, degrees=True)
    try:
        ant = mini.get_present_antenna_joint_positions()
        start_antennas = (float(ant[0]), float(ant[1]))
    except Exception:
        start_antennas = (0.0, 0.0)
    return BreathingMove(start_pose, start_antennas, interpolation_duration=0.5)


def motion_loop(mini: ReachyMini,
                state: SharedState,
                stop: threading.Event,
                stats: LoopFreqStats) -> None:
    """100 Hz: evaluate current primary + offsets, call set_target.

    When the current primary move expires (finite duration reached) the
    loop installs a fresh BreathingMove seeded from the current pose.
    """
    next_tick = time.monotonic()
    last_tick: Optional[float] = None

    while not stop.is_set():
        now = time.monotonic()
        if last_tick is not None:
            stats.add_period(now - last_tick)
        last_tick = now

        primary, t = state.get_primary_move()
        if primary is None:
            # Should not happen — main() sets one before starting the
            # motion thread. Be defensive: install fallback breathing.
            state.set_primary_move(_make_fallback_breathing(mini))
            primary, t = state.get_primary_move()

        # If the current move has expired, fall back to breathing.
        if t >= primary.duration:
            state.set_primary_move(_make_fallback_breathing(mini))
            primary, t = state.get_primary_move()
            log_player.debug("primary move expired -> breathing resumes")

        try:
            primary_head, primary_antennas, primary_yaw = primary.evaluate(t)
        except Exception as e:
            log_player.warning(
                "primary.evaluate(%.2f) failed for %s: %s -- fallback to breathing",
                t, type(primary).__name__, e)
            state.set_primary_move(_make_fallback_breathing(mini))
            primary, t = state.get_primary_move()
            primary_head, primary_antennas, primary_yaw = primary.evaluate(t)

        if primary_yaw is None:
            primary_yaw = 0.0

        ox, oy, oz, orll, opit, oyaw = state.get_offsets()
        if any(v != 0.0 for v in (ox, oy, oz, orll, opit, oyaw)):
            secondary_head = create_head_pose(
                x=ox, y=oy, z=oz,
                roll=orll, pitch=opit, yaw=oyaw,
                degrees=False, mm=False,
            )
            try:
                combined_head = compose_world_offset(
                    primary_head, secondary_head, reorthonormalize=False
                )
            except Exception as e:
                log_player.debug("compose_world_offset fallback (%s); translation-only", e)
                combined_head = primary_head.copy()
                combined_head[:3, 3] = combined_head[:3, 3] + np.array([ox, oy, oz])
        else:
            combined_head = primary_head

        try:
            mini.set_target(
                head=combined_head,
                antennas=primary_antennas,
                body_yaw=primary_yaw,
            )
        except Exception as e:
            now2 = time.monotonic()
            if now2 - getattr(motion_loop, "_last_err", 0.0) > 1.0:
                log_player.warning("set_target failed: %s", e)
                motion_loop._last_err = now2

        next_tick += MOTION_PERIOD_S
        delay = next_tick - time.monotonic()
        if delay > 0:
            stop.wait(timeout=delay)
        else:
            next_tick = time.monotonic()


# ===================================================================== #
#  Audio player                                                         #
# ===================================================================== #

def audio_player(mini: ReachyMini,
                 audio_q: "queue.Queue[np.ndarray | None]",
                 stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            samples = audio_q.get(timeout=0.2)
        except queue.Empty:
            continue
        if samples is None:
            break
        try:
            mini.media.push_audio_sample(samples)
        except Exception as e:
            log_player.warning("push_audio_sample failed: %s", e)


# ===================================================================== #
#  Motion command dispatch                                              #
# ===================================================================== #

def _handle_motion(cmd: dict, mini: ReachyMini, state: SharedState,
                   recorded_moves, dances_available) -> None:
    """Parse a motion-command JSON dict and swap the primary move.

    Schema (all UTF-8 JSON):
        {"type": "emotion", "name": "amazed1"}
        {"type": "dance",   "name": "yeah_nod"}
        {"type": "dance",   "name": "yeah_nod", "repeat": 2}   (Phase 3B: repeat ignored,
                                                                we play once; if you want
                                                                repetition, queue the call N times)
        {"type": "head",    "direction": "left"}    direction ∈ left/right/up/down/front
        {"type": "stop"}                            cancel current move → fresh breathing
    """
    kind = cmd.get("type")
    try:
        if kind == "emotion":
            name = cmd.get("name", "")
            if not _RECORDED_MOVES_AVAILABLE or recorded_moves is None:
                log_player.warning("motion: emotion ignored (library unavailable)")
                return
            if name not in recorded_moves.list_moves():
                log_player.warning("motion: unknown emotion %r; ignored", name)
                return
            move = EmotionMove(name, recorded_moves)
            state.set_primary_move(move)
            log_player.info("motion: emotion=%s (dur=%.2fs)", name, move.duration)
        elif kind == "dance":
            name = cmd.get("name", "")
            if not _DANCES_AVAILABLE or DanceMove is None:
                log_player.warning("motion: dance ignored (library unavailable)")
                return
            if name not in dances_available:
                log_player.warning("motion: unknown dance %r; ignored", name)
                return
            move = DanceMoveWrapper(name)
            state.set_primary_move(move)
            log_player.info("motion: dance=%s (dur=%.2fs)", name, move.duration)
        elif kind == "head":
            direction = cmd.get("direction", "front")
            if direction not in HEAD_DELTAS:
                log_player.warning("motion: unknown head direction %r; ignored", direction)
                return
            target = create_head_pose(*HEAD_DELTAS[direction], degrees=True)
            try:
                start_pose = np.asarray(mini.get_current_head_pose(), dtype=np.float64)
            except Exception:
                start_pose = create_head_pose(0, 0, 0, 0, 0, 0, degrees=True)
            move = GotoMove(start_pose, target, duration=GOTO_DURATION_S)
            state.set_primary_move(move)
            log_player.info("motion: head=%s (dur=%.2fs)", direction, move.duration)
        elif kind == "stop":
            state.set_primary_move(_make_fallback_breathing(mini))
            log_player.info("motion: stop (-> breathing)")
        else:
            log_player.warning("motion: unknown type %r; ignored", kind)
    except Exception:
        log_player.exception("motion dispatch failed for %r", kind)


# ===================================================================== #
#  Main / stdin reader                                                  #
# ===================================================================== #

def main() -> int:
    stdin_raw = sys.stdin.buffer
    _emit_player_startup()

    log_player.info("starting ReachyMini context…")
    shutdown_reason = "eof"
    with ReachyMini() as mini:
        sr = mini.media.get_output_audio_samplerate()
        log_player.info("sample_rate=%s", sr)
        mini.media.start_playing()

        # Seed BreathingMove
        try:
            start_pose = np.asarray(mini.get_current_head_pose(), dtype=np.float64)
        except Exception as e:
            log_player.warning("get_current_head_pose failed (%s); seeding from neutral", e)
            start_pose = create_head_pose(0, 0, 0, 0, 0, 0, degrees=True)
        try:
            ant = mini.get_present_antenna_joint_positions()
            start_antennas = (float(ant[0]), float(ant[1]))
        except Exception as e:
            log_player.warning("get_present_antenna_joint_positions failed (%s); using neutral", e)
            start_antennas = (0.0, 0.0)

        initial_primary = BreathingMove(start_pose, start_antennas,
                                        interpolation_duration=1.0)
        state = SharedState()
        state.set_primary_move(initial_primary)

        # Pre-load emotion / dance libraries. These imports happened at the
        # top of the file; here we instantiate RecordedMoves so the HF
        # snapshot fetch (if any) happens before "ready" is logged — keeps
        # the first tool call snappy and out of the audio path.
        recorded_moves = None
        if _RECORDED_MOVES_AVAILABLE:
            try:
                recorded_moves = RecordedMoves(
                    "pollen-robotics/reachy-mini-emotions-library"
                )
                emo_count = len(recorded_moves.list_moves())
                log_player.info("emotions library loaded (%d emotions)", emo_count)
            except Exception as e:
                log_player.warning("RecordedMoves init failed: %s", e)
                recorded_moves = None
        else:
            log_player.warning("emotions library unavailable (import failed)")

        dances_available = DANCES_AVAILABLE_MOVES if _DANCES_AVAILABLE else {}
        log_player.info("dances library: %d dances", len(dances_available))

        tapper = SwayRollRT()
        audio_q: "queue.Queue[np.ndarray | None]" = queue.Queue(maxsize=256)
        stop = threading.Event()
        stats = LoopFreqStats()

        audio_th = threading.Thread(
            target=audio_player, args=(mini, audio_q, stop),
            name="audio-player", daemon=True,
        )
        motion_th = threading.Thread(
            target=motion_loop, args=(mini, state, stop, stats),
            name="motion-loop", daemon=True,
        )
        audio_th.start()
        motion_th.start()

        # The laptop drainer detects readiness by matching ".endswith(' ready')".
        # Keep the literal word ready at end of message.
        log_player.info("ready")

        turn = 0
        chunks_in_turn = 0
        samples_in_turn = 0
        rc = 0
        try:
            while True:
                header = read_exact(stdin_raw, HEADER_LEN)
                if header is None:
                    log_xport.info("eof on stdin")
                    shutdown_reason = "eof"
                    break
                (length,) = struct.unpack(">I", header)

                if length == TURN_END:
                    turn += 1
                    log_xport.debug("player.frame_recv sentinel=TURN_END bytes=0")
                    log_xport.info("turn %d ended  chunks=%d samples=%d",
                                   turn, chunks_in_turn, samples_in_turn)
                    if samples_in_turn > 0:
                        log_player.info("player.audio.play_end")
                    chunks_in_turn = 0
                    samples_in_turn = 0
                    # Zero speech-sway so the head returns to pure breathing
                    # during dead air; otherwise the last hop's offset stays
                    # frozen until the next turn's first audio hop.
                    state.clear_offsets()
                    continue

                if length == CLEAR_BUFFER:
                    log_xport.debug("player.frame_recv sentinel=CLEAR bytes=0")
                    log_xport.info("clear_player")
                    try:
                        mini.media.audio.clear_player()
                    except Exception as e:
                        log_xport.warning("clear_player failed: %s", e)
                    state.clear_offsets()
                    continue

                if length == LISTENING_START:
                    log_xport.debug("player.frame_recv sentinel=LISTENING_START bytes=0")
                    log_xport.info("listening_start")
                    state.set_listening(True)
                    continue

                if length == LISTENING_END:
                    log_xport.debug("player.frame_recv sentinel=LISTENING_END bytes=0")
                    log_xport.info("listening_end")
                    state.set_listening(False)
                    continue

                if length == MOTION_COMMAND:
                    # Second 4-byte length, then JSON payload.
                    plen_hdr = read_exact(stdin_raw, HEADER_LEN)
                    if plen_hdr is None:
                        log_xport.warning("EOF reading motion command payload length")
                        shutdown_reason = "eof"
                        break
                    (plen,) = struct.unpack(">I", plen_hdr)
                    if plen <= 0 or plen > 65536:
                        log_xport.warning("motion command payload length out of range: %d", plen)
                        shutdown_reason = "bad_motion_length"
                        break
                    log_xport.debug("player.frame_recv sentinel=MOTION bytes=%d", plen)
                    payload = read_exact(stdin_raw, plen)
                    if payload is None:
                        log_xport.warning("EOF reading motion command payload")
                        shutdown_reason = "eof"
                        break
                    try:
                        cmd = json.loads(payload.decode("utf-8"))
                    except Exception as e:
                        log_xport.warning("bad motion command JSON: %s", e)
                        continue
                    _handle_motion(cmd, mini, state, recorded_moves, dances_available)
                    continue

                # Audio payload — same as Phase 3A
                log_xport.debug("player.frame_recv sentinel=audio bytes=%d", length)
                payload = read_exact(stdin_raw, length)
                if payload is None:
                    log_xport.warning("unexpected EOF mid-payload (expected %d bytes)", length)
                    shutdown_reason = "eof_mid_payload"
                    break

                samples = np.frombuffer(payload, dtype=np.float32)
                if chunks_in_turn == 0 and samples.size > 0:
                    # First audio chunk of a new turn (sr is float-pcm; duration
                    # for play_start is the first-chunk duration, not the turn's).
                    duration_ms = int(samples.size / float(sr) * 1000)
                    log_player.info("player.audio.play_start samples=%d duration_ms=%d",
                                    samples.size, duration_ms)
                try:
                    hops = tapper.feed(samples, sr=TAPPER_SR)
                except Exception as e:
                    log_player.warning("tapper.feed failed: %s", e)
                    hops = []
                if hops:
                    state.set_offsets_from_hop(hops[-1])

                try:
                    audio_q.put(samples.copy(), timeout=2.0)
                except queue.Full:
                    log_player.warning("audio queue full; dropping chunk")
                chunks_in_turn += 1
                samples_in_turn += len(samples)
        except KeyboardInterrupt:
            log_player.info("interrupted")
            shutdown_reason = "interrupted"
        except Exception as e:
            log_player.exception("player.error: %s", e)
            shutdown_reason = "error"
            rc = 1
        finally:
            stop.set()
            try:
                audio_q.put_nowait(None)
            except queue.Full:
                pass
            audio_th.join(timeout=3.0)
            motion_th.join(timeout=2.0)
            log_player.info("motion_loop_stats: %s", stats.summary())
            try:
                mini.media.stop_playing()
            except Exception as e:
                log_player.warning("stop_playing failed: %s", e)

    log_player.info("player.shutdown reason=%s", shutdown_reason)
    # The laptop drainer also matches ".endswith(' exit')".
    log_player.info("exit")
    return rc


if __name__ == "__main__":
    sys.exit(main())
