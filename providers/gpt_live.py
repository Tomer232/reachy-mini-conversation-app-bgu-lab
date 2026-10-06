#!/usr/bin/env python3
"""`gpt-live-1` -- OpenAI's full-duplex voice model, fitted to a turn loop.

Protocol (developers.openai.com, guides/voice-websockets?api=live and
guides/live-conversations, read 2026-10-04):

    wss://api.openai.com/v1/live/sessions      Authorization: Bearer <key>
    -> session.start {model, instructions, audio:{format, output:{voice}}}
    <- session.started
    -> session.input_audio.append {audio: base64 PCM16}      (continuously)
    <- session.output_audio.delta {delta}
    <- session.input_transcript.delta / session.output_transcript.delta
    <- session.delegation.created  (client mode: delegation omitted)
    <- session.closed {reason}     <- error {message}

**What it is not.** It is not a turn-based API, and this file does not pretend
otherwise; it adapts. GPT-LIVE-MIGRATION-PLAN.md 4.1 is the background:

  * **It must hear the user live.** OpenAI's guide: "Piping an entire file at
    once does not simulate a live microphone" -- audio is consumed at the
    sample rate. Sending the finished recording afterwards, the way Gemini
    gets it, would cost the user's whole utterance again in latency. So the
    capture taps every frame to this session as it is recorded
    (`mic_streamer`), and the model decides for itself when the user is done.
  * **Silence while the robot talks.** The uplink never stops -- when no mic
    frame has arrived for a moment it sends digital silence at real-time pace.
    The mic is closed while the robot speaks (the turn loop is half-duplex),
    so the model hears silence rather than itself. That deletes the echo
    problem (plan 4.3) and, honestly, barge-in with it. Barge-in is not
    something this robot does today on any backend.
  * **There is no end-of-reply event.** "Track playback in your client." The
    turn ends here when no output audio has arrived for END_GAP_S. That is a
    guess where Gemini gives a fact, and it is tunable
    (REACHY_GPT_LIVE_END_GAP_S) because it has to be tuned on hardware.
  * **No motion tools.** Tools live behind delegation, a second model round
    trip that would land gestures seconds after the words (plan 4.2). The
    robot still sways to its own speech -- that runs on the robot, off the
    audio, for every backend. Delegation is left in client mode and any
    request is answered "no tools, answer yourself".
  * **Hebrew is unverified.** OpenAI publishes no language list and all twelve
    voices are English or Brazilian Portuguese. Instructions are written in the
    target language, which OpenAI's prompting guide says is how you get it.
    The dashboard says the rest out loud.

Camera vision (vision.py): gpt-live-1 takes no images, so a side model
describes the newest camera frame and the description is added to the session
as quiet context (`session.thinking.append`, delegation_id null: "general
session context", <= 500 tokens) as each turn's mic opens. Fire-and-forget:
it never delays a reply. See `attach_vision`.

Reconnect: on `session.closed` (expired / connection_lost) the next turn opens
a fresh session and restores the conversation so far via `session.input`.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import itertools
import json
import logging
import os
import time
from typing import Any, Callable, Optional

import numpy as np

from .base import ProviderUnavailable, SpeechProvider, gemini_like

log = logging.getLogger("reachy.provider.gpt_live")

LIVE_URL = os.environ.get("REACHY_GPT_LIVE_URL",
                          "wss://api.openai.com/v1/live/sessions")
RATE = 24000                 # session audio rate, both directions; the turn
                             # loop and the robot path both expect 24 kHz out
MIC_RATE = 16000             # what the capture taps us with
SILENCE_AFTER_MIC_S = 0.3    # mic quiet this long -> start sending silence
SILENCE_CHUNK_S = 0.1
PUMP_TICK_S = 0.02
START_TIMEOUT_S = 15.0
# No *audible* output audio for this long after the reply began = the reply is
# over. 1.2 s cut a Hebrew joke at its comic pause (2026-10-05).
END_GAP_S = float(os.environ.get("REACHY_GPT_LIVE_END_GAP_S", "2.0"))
# The output stream never stops: the model sends digital silence while it
# listens and after it has spoken (measured 2026-10-05 on reachy3: a reply
# opened with 4 s of zeros, and silence kept the turn open until it was
# cancelled). A chunk whose loudest sample is under this is silence: it does
# not start a reply and does not keep one alive.
SILENT_PEAK = int(os.environ.get("REACHY_GPT_LIVE_SILENT_PEAK", "300"))
# The reply's first audio is held until this much has arrived. gpt-live-1
# streams at about real-time pace, so a player that starts on the first 64 ms
# runs dry on the slightest network jitter -- heard as a cut in the first
# word or sentence, never later once a little has built up (reachy2,
# 2026-10-05). Gemini arrives in bursts and never needed this.
PREBUFFER_S = float(os.environ.get("REACHY_GPT_LIVE_PREBUFFER_S", "0.35"))
PREBUFFER_BYTES = int(PREBUFFER_S * RATE) * 2
# ...but never held longer than this after the first audible chunk.
PREBUFFER_MAX_WAIT_S = 0.6
# Extra wait, after the reply ends, for the user's own transcript to land, so
# the dashboard shows what was heard. Bounded so it never holds a turn up.
USER_TEXT_GRACE_S = 0.8
# Camera notes (attach_vision): never two describes at once, and none sooner
# than this after the last, so a quick back-and-forth does not stack notes.
LOOK_MIN_GAP_S = 4.0

# History carried into a reconnected session (OpenAI caps 128 msgs / 8k tok).
HISTORY_MAX_MESSAGES = 40
HISTORY_MAX_CHARS = 6000

_POLICY = {
    "he": (
        "\n\nכללי שיחה קולית: המתן עד שהמשתמש מסיים לדבר ורק אז ענה. "
        "אל תשמיע קולות הקשבה כמו 'אהה' או 'מממ' בזמן שהמשתמש מדבר, "
        "ואל תתחיל לדבר לפני שהמשתמש פנה אליך. "
        "בשיחה הזאת אין לך כלי תנועה ואין גישה לחיפוש או לכלים אחרים — "
        "פשוט דבר, וענה מהידע שלך. דבר עברית טבעית."
    ),
    "en": (
        "\n\nVoice conversation rules: wait until the user has finished "
        "speaking, then answer. Do not make listening sounds like 'uh-huh' or "
        "'mm' while the user talks, and do not start speaking before the user "
        "has addressed you. In this session you have no movement tools and no "
        "search or other tools -- just talk, and answer from what you know."
    ),
}

_NO_TOOLS_REPLY = (
    "No backend or tools are available in this session. Answer directly from "
    "your own knowledge, briefly, in the conversation's language.")


def _lang(language: str) -> str:
    return "he" if str(language or "").lower().startswith("he") else "en"


class _Upsampler:
    """16 kHz int16 frames -> 24 kHz int16, continuous across frames.

    Resampling each 32 ms frame on its own would put a filter edge every
    32 ms -- a 31 Hz click train straight into the model's ASR. This holds one
    frame back and resamples it with real neighbours on both sides.
    """

    CTX = 64   # input samples of context each side; even, so 3/2 maps cleanly

    def __init__(self) -> None:
        self._prev = np.zeros(0, dtype=np.float32)
        self._pending: Optional[np.ndarray] = None

    def _resample(self, left: np.ndarray, mid: np.ndarray,
                  right: np.ndarray) -> np.ndarray:
        from scipy.signal import resample_poly
        x = np.concatenate([left, mid, right])
        y = resample_poly(x, 3, 2)
        start = len(left) * 3 // 2
        out = y[start:start + len(mid) * 3 // 2]
        return np.clip(out * 32768.0, -32768.0, 32767.0).astype(np.int16)

    def push(self, frame_int16: np.ndarray) -> Optional[np.ndarray]:
        f = frame_int16.astype(np.float32) / 32768.0
        if f.size % 2:
            f = f[:-1]
        out = None
        if self._pending is not None:
            out = self._resample(self._prev[-self.CTX:], self._pending,
                                 f[:self.CTX])
            self._prev = self._pending
        self._pending = f
        return out

    def flush(self) -> Optional[np.ndarray]:
        if self._pending is None:
            return None
        out = self._resample(self._prev[-self.CTX:], self._pending,
                             np.zeros(0, dtype=np.float32))
        self._prev = np.zeros(0, dtype=np.float32)
        self._pending = None
        return out


class GptLiveClient:
    """What `make_client` returns: just the key. The session owns the socket."""

    def __init__(self, key: str) -> None:
        self.key = key


class GptLiveSession:
    """One gpt-live-1 conversation, wearing a Gemini session's interface."""

    def __init__(self, key: str, start_session: dict) -> None:
        self._key = key
        self._start_session = start_session
        self._ws = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._events: "asyncio.Queue" = asyncio.Queue()
        self._mic_q: "asyncio.Queue" = asyncio.Queue()
        self._reader_task: Optional[asyncio.Task] = None
        self._pump_task: Optional[asyncio.Task] = None
        self._closed_reason: Optional[str] = None
        self._ids = itertools.count(1)
        self._history: list = []
        self._reconnect_lock = asyncio.Lock()
        self.session_id = ""
        # Camera vision, when the turn loop attaches it (attach_vision).
        self._feed = None
        self._describer = None
        self._look_task: Optional[asyncio.Task] = None
        self._last_look_t = 0.0
        self._last_view = ""
        self.views_sent = 0

    # ----- lifecycle -----

    async def open(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self._connect(restore=False)
        self._pump_task = asyncio.create_task(self._pump(), name="gpt-live-uplink")

    async def _connect(self, restore: bool) -> None:
        import websockets
        session = dict(self._start_session)
        if restore and self._history:
            session["input"] = self._history_items()
        try:
            ws = await websockets.connect(
                LIVE_URL,
                additional_headers={"Authorization": "Bearer " + self._key},
                max_size=None, open_timeout=START_TIMEOUT_S)
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 401:
                raise ProviderUnavailable(
                    "OpenAI rejected the API key (401: invalid or revoked key). "
                    "Make a new one at platform.openai.com/api-keys") from e
            if status == 403:
                raise ProviderUnavailable(
                    "OpenAI refused access to gpt-live-1 for this key (403) -- "
                    "check the project's model access and billing tier") from e
            raise ProviderUnavailable(
                "could not reach OpenAI's live endpoint: {}".format(e)) from e
        await ws.send(json.dumps({"type": "session.start",
                                  "event_id": self._event_id("start"),
                                  "session": session}))
        deadline = time.monotonic() + START_TIMEOUT_S
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                await ws.close()
                raise ProviderUnavailable(
                    "gpt-live-1 did not confirm the session within {:.0f}s"
                    .format(START_TIMEOUT_S))
            try:
                raw = await asyncio.wait_for(ws.recv(), left)
            except Exception as e:
                raise ProviderUnavailable(
                    "gpt-live-1 closed the connection while starting: {}"
                    .format(_close_detail(ws, e))) from e
            msg = _parse(raw)
            kind = msg.get("type", "")
            if kind == "session.started":
                self.session_id = str((msg.get("session") or {}).get("id") or "")
                break
            if kind == "error":
                await ws.close()
                raise ProviderUnavailable(
                    "gpt-live-1 refused the session: {}".format(_error_text(msg)))
            log.debug("pre-start event %s", kind)
        self._ws = ws
        self._closed_reason = None
        log.info("gpt-live-1 session %s (%s)", self.session_id or "?",
                 "restored with {} messages".format(len(self._history))
                 if restore and self._history else "new")
        self._reader_task = asyncio.create_task(self._reader(ws),
                                                name="gpt-live-reader")

    async def close(self) -> None:
        if self._look_task is not None and not self._look_task.done():
            self._look_task.cancel()
        for task in (self._pump_task, self._reader_task):
            if task is not None and not task.done():
                task.cancel()
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.send(json.dumps({"type": "session.close",
                                          "event_id": self._event_id("close")}))
                # Give the server a moment to answer with session.closed; the
                # socket is closed either way.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(_until_closed(ws), 3.0)
            except Exception:
                pass
            with contextlib.suppress(Exception):
                await ws.close()
        for task in (self._pump_task, self._reader_task):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def _ensure_open(self) -> None:
        if self._ws is not None and self._closed_reason is None:
            return
        async with self._reconnect_lock:
            if self._ws is not None and self._closed_reason is None:
                return
            log.warning("gpt-live-1 session closed (%s); reconnecting",
                        self._closed_reason or "not open")
            if self._ws is not None:
                with contextlib.suppress(Exception):
                    await self._ws.close()
                self._ws = None
            await self._connect(restore=True)

    # ----- the half of the Gemini interface the turn loop sends on -----

    def mic_streamer(self) -> Callable[[np.ndarray], None]:
        """Called by the turn loop as the mic opens for a new turn.

        Anything still queued from before this moment belongs to an earlier
        reply (a tail that arrived after the turn was declared over); dropping
        it here is what stops the robot opening a new turn with the end of the
        last one.
        """
        stale = 0
        while not self._events.empty():
            self._events.get_nowait()
            stale += 1
        if stale:
            log.debug("dropped %d stale event(s) at mic open", stale)
        # The person is about to speak: show the model what is in front of
        # the robot now, so a "what am I holding?" has an answer waiting.
        self._look("turn")
        loop = self._loop

        def feed(frame_int16_16k: np.ndarray) -> None:
            # Called on the capture worker thread.
            loop.call_soon_threadsafe(self._mic_q.put_nowait,
                                      np.array(frame_int16_16k, copy=True))
        return feed

    async def send_realtime_input(self, audio=None, audio_stream_end=None,
                                  **_ignored) -> None:
        """A no-op by design: the audio was streamed live as it was spoken.

        Kept so the turn loop's send path, and its send_failed handling, work
        unchanged. It does reopen a session that closed between turns, so a
        dead socket surfaces here rather than as a silent drain.
        """
        await self._ensure_open()

    # ----- camera vision -----

    def attach_vision(self, feed, describer) -> None:
        """Called once by the turn loop after the session opens, when the
        dashboard's Camera vision switch is on and a camera exists. `feed` is
        a vision.CameraFeed, `describer` a vision.SceneDescriber."""
        self._feed, self._describer = feed, describer
        self._look("start")

    def _look(self, why: str) -> None:
        if self._feed is None or self._describer is None:
            return
        if self._look_task is not None and not self._look_task.done():
            return
        if time.monotonic() - self._last_look_t < LOOK_MIN_GAP_S:
            return
        frame = self._feed.latest()
        if frame is None:
            log.info("vision: no camera frame yet (%s)", why)
            return
        self._last_look_t = time.monotonic()
        self._look_task = asyncio.get_running_loop().create_task(
            self._describe_and_tell(frame, why), name="gpt-live-look")

    async def _describe_and_tell(self, frame, why: str) -> None:
        t0 = time.monotonic()
        text = await self._describer.describe(frame)
        if not text:
            return
        self._last_view = text
        ws = self._ws
        if ws is None or self._closed_reason is not None:
            return
        try:
            await ws.send(json.dumps({
                "type": "session.thinking.append",
                "event_id": self._event_id("look"),
                "delegation_id": None,
                "content": "What your camera sees right now (replaces any "
                           "earlier camera note): " + text,
            }))
        except Exception as e:  # noqa: BLE001
            log.warning("vision: could not add the camera note: %s", e)
            return
        self.views_sent += 1
        log.info("vision: camera note #%d (%s) after %.1fs, frame %dx%d %d B "
                 "%.1fs old: %s", self.views_sent, why, time.monotonic() - t0,
                 frame.width, frame.height, len(frame.jpeg),
                 time.monotonic() - frame.t, text[:120])

    async def send_tool_response(self, **_ignored) -> None:
        """No tools are declared, so nothing ever calls this. Present for the
        interface."""

    # ----- the half the turn loop receives on -----

    async def receive(self):
        """One reply, as Gemini-shaped messages, ending in turn_complete.

        Over when no output audio has arrived for END_GAP_S -- plus, if the
        user's own transcript has not landed yet, up to USER_TEXT_GRACE_S more
        for it. Audio arriving inside that grace means the reply was not over
        after all, and the clock starts again.
        """
        audio_seen = False
        last_audio = 0.0
        grace_until: Optional[float] = None
        user_parts: list = []
        robot_parts: list = []
        held: list = []                 # the prebuffer, see PREBUFFER_S
        holding = PREBUFFER_BYTES > 0
        release_at = 0.0
        while True:
            now = time.monotonic()
            if held and (sum(len(b) for b in held) >= PREBUFFER_BYTES
                         or now >= release_at):
                holding = False
                yield gemini_like(audio=b"".join(held))
                held = []
            if audio_seen and now - last_audio >= END_GAP_S:
                if user_parts or (grace_until is not None and now >= grace_until):
                    if held:
                        yield gemini_like(audio=b"".join(held))
                        held = []
                    self._remember("".join(user_parts), "".join(robot_parts))
                    yield gemini_like(turn_complete=True)
                    return
                if grace_until is None:
                    grace_until = now + USER_TEXT_GRACE_S
                wait = grace_until - now
            elif audio_seen:
                wait = END_GAP_S - (now - last_audio)
            else:
                # Nothing yet. The turn loop's watchdog owns giving up.
                wait = 0.5
            if held:
                wait = min(wait, max(release_at - now, 0.0))
            try:
                kind, payload = await asyncio.wait_for(self._events.get(),
                                                       max(wait, 0.01))
            except asyncio.TimeoutError:
                continue
            if kind == "audio":
                if not _audible(payload):
                    # Silence before the reply is dropped (it would only delay
                    # the robot); silence inside it is kept as the pause it is,
                    # but neither keeps the turn open.
                    if audio_seen:
                        if holding:
                            held.append(payload)
                        else:
                            yield gemini_like(audio=payload)
                    continue
                if not audio_seen:
                    release_at = time.monotonic() + PREBUFFER_MAX_WAIT_S
                audio_seen = True
                last_audio = time.monotonic()
                grace_until = None
                if holding:
                    held.append(payload)
                    continue
                yield gemini_like(audio=payload)
            elif kind == "user_text":
                user_parts.append(payload)
                yield gemini_like(user_text=payload)
            elif kind == "robot_text":
                robot_parts.append(payload)
                yield gemini_like(robot_text=payload)
            elif kind == "closed":
                if audio_seen:
                    # The session ended (expired, say) after the reply was
                    # spoken. The reply stands; the next turn reconnects.
                    log.info("gpt-live-1 session closed (%s) after the reply; "
                             "keeping it", payload)
                    if held:
                        yield gemini_like(audio=b"".join(held))
                        held = []
                    self._remember("".join(user_parts), "".join(robot_parts))
                    yield gemini_like(turn_complete=True)
                    return
                raise ConnectionError(
                    "gpt-live-1 session closed before replying ({})".format(payload))

    # ----- background tasks -----

    async def _reader(self, ws) -> None:
        carry = b""   # a split 16-bit sample, should a delta ever end mid-sample
        try:
            async for raw in ws:
                msg = _parse(raw)
                kind = msg.get("type", "")
                if kind == "session.output_audio.delta":
                    data = msg.get("delta") or ""
                    if data:
                        pcm = carry + base64.b64decode(data)
                        cut = len(pcm) & ~1
                        carry = pcm[cut:]
                        if cut:
                            self._events.put_nowait(("audio", pcm[:cut]))
                elif kind == "session.input_transcript.delta":
                    if msg.get("delta"):
                        self._events.put_nowait(("user_text", msg["delta"]))
                elif kind == "session.output_transcript.delta":
                    if msg.get("delta"):
                        self._events.put_nowait(("robot_text", msg["delta"]))
                elif kind == "session.delegation.created":
                    await self._decline_delegation(ws, msg)
                elif kind == "session.thinking.appended":
                    log.debug("gpt-live-1 accepted a context note")
                elif kind == "session.closed":
                    reason = str(msg.get("reason") or "closed")
                    self._closed_reason = reason
                    log.warning("gpt-live-1 closed the session: %s", reason)
                    self._events.put_nowait(("closed", reason))
                    return
                elif kind == "error":
                    log.warning("gpt-live-1 error: %s", _error_text(msg))
                else:
                    log.debug("gpt-live-1 event %s", kind)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("gpt-live-1 connection lost: %s", e)
        if self._closed_reason is None:
            self._closed_reason = "connection_lost"
            self._events.put_nowait(("closed", "connection_lost"))

    async def _decline_delegation(self, ws, msg: dict) -> None:
        delegation = msg.get("delegation") or {}
        did = delegation.get("id")
        log.info("gpt-live-1 asked to delegate (%s); telling it no tools exist", did)
        content = _NO_TOOLS_REPLY
        if self._last_view:
            # It may have wanted to look: the newest camera note is the answer.
            content += " What your camera sees right now: " + self._last_view
        with contextlib.suppress(Exception):
            await ws.send(json.dumps({
                "type": "session.thinking.append",
                "event_id": self._event_id("nodeleg"),
                "delegation_id": did,
                "content": content,
            }))

    async def _pump(self) -> None:
        """The uplink: mic frames while the user can talk, silence otherwise,
        at real-time pace, for as long as the session lives."""
        up = _Upsampler()
        last_mic = 0.0
        sent_until = time.monotonic()
        try:
            while True:
                await asyncio.sleep(PUMP_TICK_S)
                frames = []
                while not self._mic_q.empty():
                    frames.append(self._mic_q.get_nowait())
                now = time.monotonic()
                chunks = []
                if frames:
                    last_mic = now
                    for fr in frames:
                        out = up.push(fr)
                        if out is not None:
                            chunks.append(out)
                    sent_until = now
                elif now - last_mic > SILENCE_AFTER_MIC_S:
                    tail = up.flush()
                    if tail is not None:
                        chunks.append(tail)
                        sent_until = now
                    owed = now - sent_until
                    if owed >= SILENCE_CHUNK_S:
                        owed = min(owed, 1.0)
                        chunks.append(np.zeros(int(owed * RATE) & ~1, dtype=np.int16))
                        sent_until = now
                if not chunks:
                    continue
                if self._ws is None or self._closed_reason is not None:
                    try:
                        await self._ensure_open()
                    except Exception as e:
                        log.warning("gpt-live-1 reconnect failed: %s", e)
                        await asyncio.sleep(1.0)
                        continue
                pcm = np.concatenate(chunks).tobytes()
                try:
                    await self._ws.send(json.dumps({
                        "type": "session.input_audio.append",
                        "audio": base64.b64encode(pcm).decode("ascii"),
                    }))
                except Exception as e:
                    if self._closed_reason is None:
                        self._closed_reason = "send_failed: {}".format(e)
        except asyncio.CancelledError:
            return

    # ----- history, for reconnects -----

    def _remember(self, user_text: str, robot_text: str) -> None:
        if user_text.strip():
            self._history.append(("user", user_text.strip()))
        if robot_text.strip():
            self._history.append(("assistant", robot_text.strip()))
        del self._history[:-HISTORY_MAX_MESSAGES]
        while (sum(len(t) for _, t in self._history) > HISTORY_MAX_CHARS
               and len(self._history) > 2):
            del self._history[0]

    def _history_items(self) -> list:
        return [{"type": "message", "role": role,
                 "content": [{"type": "input_text" if role == "user"
                              else "output_text", "text": text}]}
                for role, text in self._history]

    def _event_id(self, tag: str) -> str:
        return "evt_{}_{}".format(tag, next(self._ids))


def _audible(pcm: bytes) -> bool:
    """True if this PCM16 chunk has any sample at or over SILENT_PEAK."""
    samples = np.frombuffer(pcm, dtype=np.int16)
    return samples.size > 0 and int(np.abs(samples.astype(np.int32)).max()) >= SILENT_PEAK


def _parse(raw) -> dict:
    try:
        msg = json.loads(raw)
        return msg if isinstance(msg, dict) else {}
    except Exception:
        return {}


def _error_text(msg: dict) -> str:
    err = msg.get("error") if isinstance(msg.get("error"), dict) else msg
    parts = [str(err.get(k)) for k in ("code", "type", "message") if err.get(k)]
    return " / ".join(parts) or json.dumps(msg)[:300]


def _close_detail(ws, exc) -> str:
    code = getattr(ws, "close_code", None)
    reason = getattr(ws, "close_reason", None)
    if code or reason:
        return "code {} {}".format(code, reason or "").strip()
    return str(exc)


async def _until_closed(ws) -> None:
    async for raw in ws:
        if _parse(raw).get("type") == "session.closed":
            return


class GptLiveProvider(SpeechProvider):
    name = "gpt_live"
    display_name = "GPT-Live-1"
    default_model = "gpt-live-1"
    # guides/live-conversations, 2026-10-04: ten English, two Brazilian
    # Portuguese. Gleam is a natural-sounding North American voice.
    voices = ("gleam", "meridian", "quartz", "ripple", "vesper", "willow",
              "stone", "beacon", "delta", "cinder", "bossa", "tempo")
    default_voice = "gleam"
    default_language = "en-US"
    implemented = True

    def make_client(self, credential) -> Any:
        return GptLiveClient(credential.key)

    def build_config(self, *, system_prompt: str, voice: str,
                     language: str, tools: Optional[list] = None,
                     model: str = "") -> Any:
        # `tools` is accepted and ignored: motion stays off delegation (see
        # the module docstring), and the robot sways to speech regardless.
        voice = voice if voice in self.voices else self.default_voice
        return {
            "model": model or self.default_model,
            "instructions": system_prompt + _POLICY[_lang(language)],
            "audio": {
                "format": {"type": "audio/pcm", "rate": RATE},
                "output": {"voice": voice},
            },
        }

    def connect(self, client: Any, config: Any, model: Optional[str] = None):
        start = dict(config)
        if model:
            start["model"] = model

        @contextlib.asynccontextmanager
        async def _session():
            session = GptLiveSession(client.key, start)
            await session.open()
            try:
                yield session
            finally:
                await session.close()
        return _session()

    def supports_language(self, language: str) -> bool:
        # Unverified for Hebrew: allowed, and labelled as such on the dashboard.
        return True
