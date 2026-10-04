#!/usr/bin/env python3
"""ElevenLabs v4 as the robot's voice, over whichever backend is thinking.

The dashboard's ElevenLabs switch. With it on, Gemini or gpt-live-1 still
listens, understands and decides what to say -- including the motion tools --
but the robot speaks with an ElevenLabs voice instead of the backend's own.

How (elevenlabs.io docs, read 2026-10-04):

  * Eleven v4 is `eleven_v4` / `eleven_v4_turbo`. ElevenLabs' own advice is
    `eleven_v4_turbo` "through the Text to Dialogue WebSocket for agents and
    interactive applications" (~100 ms median inference); the ordinary
    text-to-speech endpoints do not take v3/v4. So:
        wss://api.elevenlabs.io/v1/text-to-dialogue/stream-input
            ?model_id=eleven_v4_turbo&output_format=pcm_24000
        -> {"voices": [voice_id]}            exactly one voice for v4 turbo
        -> {"inputs": [{"text", "voice_id"}]} as text arrives
        -> {"flush": true}, {"close_socket": true}
        <- {"audio": base64, "is_final": bool, "error"?}
    Hebrew is in v4's 99 languages; the language is read off the text.
  * The text is the backend's own **output transcription**, streamed as it
    arrives. The backend's native audio is dropped (and still billed -- these
    are speech-to-speech models and cannot be told not to speak).
  * One socket per turn, opened the moment the user's turn is sent, so the
    connect overlaps the model's thinking time. 24 kHz PCM16 is exactly what
    the turn loop already expects from Gemini, so nothing downstream changes.

**If ElevenLabs fails, the robot still talks.** A turn whose socket could not
open passes the backend's own audio straight through, and the log says so.
A robot that goes mute because a third service hiccuped is the worst way to
find out a key expired.

**Tool-response timing.** The turn loop picks SILENT scheduling once *it* has
seen audio. Here it sees ElevenLabs audio, which trails the backend's own by
the TTS latency -- so a tool call landing in that gap would get default
scheduling, which on Gemini means answering twice (2026-07-26). The wrapper
therefore re-decides the scheduling from the backend's own audio.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import time
from typing import Any, Optional

from .base import gemini_like

log = logging.getLogger("reachy.provider.elevenlabs")

TDD_URL = os.environ.get(
    "REACHY_ELEVENLABS_URL",
    "wss://api.elevenlabs.io/v1/text-to-dialogue/stream-input")
API_BASE = "https://api.elevenlabs.io"
DEFAULT_MODEL = "eleven_v4_turbo"
MODELS = ("eleven_v4_turbo", "eleven_v4")
OUTPUT_FORMAT = "pcm_24000"
CONNECT_TIMEOUT_S = 6.0
# How long the first backend message may wait for the TTS socket before the
# turn gives up on ElevenLabs and uses the backend's own voice.
READY_WAIT_S = 3.0
# After the last text is sent, how long to wait for ElevenLabs to finish.
FINISH_TIMEOUT_S = 20.0


def list_voices(api_key: str, timeout: float = 8.0) -> list:
    """The account's voices as [{voice_id, name, category, language}]."""
    import httpx
    r = httpx.get(API_BASE + "/v1/voices", headers={"xi-api-key": api_key},
                  timeout=timeout)
    r.raise_for_status()
    out = []
    for v in (r.json().get("voices") or []):
        labels = v.get("labels") or {}
        out.append({
            "voice_id": v.get("voice_id", ""),
            "name": v.get("name", ""),
            "category": v.get("category", ""),
            "language": labels.get("language", "") or labels.get("accent", ""),
        })
    return [v for v in out if v["voice_id"]]


class ElevenLabsVoice:
    """The switch's settings for one conversation. Stateless across turns."""

    def __init__(self, api_key: str, voice_id: str,
                 model_id: str = DEFAULT_MODEL, voice_name: str = "") -> None:
        self.api_key = api_key
        self.voice_id = voice_id
        self.model_id = model_id or DEFAULT_MODEL
        self.voice_name = voice_name

    def describe(self) -> str:
        return "elevenlabs:{}:{}".format(self.model_id,
                                         self.voice_name or self.voice_id)

    def wrap(self, session):
        voice = self

        @contextlib.asynccontextmanager
        async def _wrapped():
            voiced = VoicedSession(session, voice)
            try:
                yield voiced
            finally:
                await voiced.aclose()
        return _wrapped()


class _TtsStream:
    """One turn's ElevenLabs socket."""

    def __init__(self, voice: ElevenLabsVoice) -> None:
        self.voice = voice
        self.ws = None
        self.failed: Optional[str] = None
        self.texts_sent = 0
        self.chars_sent = 0
        self.first_audio_t: Optional[float] = None
        self._audio: "asyncio.Queue" = asyncio.Queue()
        self._connect_task = asyncio.create_task(self._connect(),
                                                 name="elevenlabs-connect")
        self._reader_task: Optional[asyncio.Task] = None
        self._finished = False

    async def _connect(self) -> None:
        import websockets
        url = "{}?model_id={}&output_format={}".format(
            TDD_URL, self.voice.model_id, OUTPUT_FORMAT)
        try:
            self.ws = await websockets.connect(
                url, additional_headers={"xi-api-key": self.voice.api_key},
                max_size=None, open_timeout=CONNECT_TIMEOUT_S)
            await self.ws.send(json.dumps({"voices": [self.voice.voice_id]}))
        except Exception as e:
            self.failed = "could not open the ElevenLabs stream: {}".format(e)
            return
        self._reader_task = asyncio.create_task(self._reader(),
                                                name="elevenlabs-reader")

    async def wait_ready(self, timeout: float) -> bool:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(self._connect_task), timeout)
        if not self._connect_task.done() and self.failed is None:
            self.failed = "the ElevenLabs stream took over {:.0f}s to open".format(timeout)
        return self.failed is None and self.ws is not None

    async def send_text(self, text: str) -> None:
        if self.failed or self.ws is None or not text:
            return
        try:
            await self.ws.send(json.dumps({"inputs": [
                {"text": text, "voice_id": self.voice.voice_id}]}))
            self.texts_sent += 1
            self.chars_sent += len(text)
        except Exception as e:
            self.failed = "ElevenLabs send failed: {}".format(e)
            self._audio.put_nowait(None)

    async def finish(self) -> None:
        """No more text this turn. Audio keeps arriving until is_final."""
        if self._finished:
            return
        self._finished = True
        if self.failed or self.ws is None or self.texts_sent == 0:
            self._audio.put_nowait(None)
            return
        try:
            await self.ws.send(json.dumps({"flush": True}))
            await self.ws.send(json.dumps({"close_socket": True}))
        except Exception as e:
            log.warning("ElevenLabs finish failed: %s", e)
            self._audio.put_nowait(None)

    async def audio(self):
        """Yields PCM16 24 kHz chunks until the turn's audio is complete."""
        carry = b""
        while True:
            try:
                chunk = await asyncio.wait_for(
                    self._audio.get(),
                    FINISH_TIMEOUT_S if self._finished else None)
            except asyncio.TimeoutError:
                log.warning("ElevenLabs never finished the turn; moving on")
                return
            if chunk is None:
                return
            chunk = carry + chunk
            if len(chunk) % 2:
                carry, chunk = chunk[-1:], chunk[:-1]
            else:
                carry = b""
            if chunk:
                yield chunk

    async def _reader(self) -> None:
        try:
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if msg.get("error"):
                    self.failed = "ElevenLabs error: {}".format(msg.get("error"))
                    log.error("%s", self.failed)
                    break
                data = msg.get("audio")
                if data:
                    if self.first_audio_t is None:
                        self.first_audio_t = time.perf_counter()
                    self._audio.put_nowait(base64.b64decode(data))
                if msg.get("is_final") or msg.get("isFinal"):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not self._finished:
                self.failed = "ElevenLabs stream dropped: {}".format(e)
                log.warning("%s", self.failed)
        self._audio.put_nowait(None)

    async def close(self) -> None:
        for task in (self._connect_task, self._reader_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._connect_task, self._reader_task):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if self.ws is not None:
            with contextlib.suppress(Exception):
                await self.ws.close()


class VoicedSession:
    """A backend session whose voice is replaced. Everything not overridden
    here -- mic_streamer, for instance -- is the backend session's own."""

    def __init__(self, inner, voice: ElevenLabsVoice) -> None:
        self._inner = inner
        self._voice = voice
        self._tts: Optional[_TtsStream] = None
        self._native_audio_started = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def send_realtime_input(self, **kwargs) -> None:
        if kwargs.get("audio") is not None:
            # The user's turn is going out: open this turn's voice now, so the
            # connect runs while the model is still thinking.
            await self._drop_tts()
            self._native_audio_started = False
            self._tts = _TtsStream(self._voice)
        await self._inner.send_realtime_input(**kwargs)

    async def send_tool_response(self, function_responses=None, **kwargs) -> None:
        for fr in (function_responses or []):
            if hasattr(fr, "scheduling"):
                try:
                    from google.genai import types
                    fr.scheduling = (types.FunctionResponseScheduling.SILENT
                                     if self._native_audio_started else None)
                except Exception:
                    pass
        await self._inner.send_tool_response(
            function_responses=function_responses, **kwargs)

    async def receive(self):
        tts = self._tts or _TtsStream(self._voice)
        self._tts = tts
        out: "asyncio.Queue" = asyncio.Queue()
        passthrough = False

        async def backend_pump():
            nonlocal passthrough
            checked = False
            try:
                async for resp in self._inner.receive():
                    if not checked:
                        checked = True
                        if not await tts.wait_ready(READY_WAIT_S):
                            passthrough = True
                            log.error("%s -- this turn uses the backend's own "
                                      "voice", tts.failed)
                    if (getattr(resp, "tool_call", None) is not None
                            or getattr(resp, "tool_call_cancellation", None) is not None):
                        out.put_nowait(("msg", resp))
                        continue
                    sc = getattr(resp, "server_content", None)
                    if sc is None:
                        continue
                    native = b""
                    if sc.model_turn and sc.model_turn.parts:
                        for part in sc.model_turn.parts:
                            data = getattr(getattr(part, "inline_data", None), "data", None)
                            if data:
                                if isinstance(data, str):
                                    data = base64.b64decode(data)
                                native += data
                    if native:
                        self._native_audio_started = True
                        if passthrough:
                            out.put_nowait(("audio", native))
                    user_text = (sc.input_transcription.text
                                 if sc.input_transcription else None)
                    robot_text = (sc.output_transcription.text
                                  if sc.output_transcription else None)
                    if robot_text and not passthrough:
                        await tts.send_text(robot_text)
                    if user_text or robot_text:
                        out.put_nowait(("msg", gemini_like(user_text=user_text,
                                                           robot_text=robot_text)))
                    if sc.turn_complete:
                        break
            except Exception as e:
                out.put_nowait(("error", e))
                return
            finally:
                await tts.finish()
            out.put_nowait(("backend_done", None))

        async def voice_pump():
            async for chunk in tts.audio():
                if not passthrough:
                    out.put_nowait(("audio", chunk))
            out.put_nowait(("voice_done", None))

        tasks = [asyncio.create_task(backend_pump(), name="elevenlabs-backend"),
                 asyncio.create_task(voice_pump(), name="elevenlabs-voice")]
        backend_done = voice_done = False
        try:
            while not (backend_done and voice_done):
                kind, value = await out.get()
                if kind == "msg":
                    yield value
                elif kind == "audio":
                    yield gemini_like(audio=value)
                elif kind == "backend_done":
                    backend_done = True
                elif kind == "voice_done":
                    voice_done = True
                elif kind == "error":
                    raise value
            if tts.failed and not passthrough:
                log.error("%s", tts.failed)
            log.info("ElevenLabs turn: %d text chunk(s), %d chars%s",
                     tts.texts_sent, tts.chars_sent,
                     " (fell back to the backend's voice)" if passthrough else "")
            yield gemini_like(turn_complete=True)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            await self._drop_tts()

    async def _drop_tts(self) -> None:
        tts, self._tts = self._tts, None
        if tts is not None:
            await tts.close()

    async def aclose(self) -> None:
        await self._drop_tts()
