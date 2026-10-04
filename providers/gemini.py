#!/usr/bin/env python3
"""Gemini Live -- the path that works, moved behind the seam unchanged.

Every value and every call here came out of `conversation.py`, deliberately
without improvement. `build_config` produces the same `LiveConnectConfig` the
old module-level `build_live_config()` produced; the only difference is that
the prompt and the voice now arrive as arguments instead of being read off
module constants, which is exactly what the persona switch needed.

**Why the constants are still imported from `conversation` rather than owned
here.** `GEMINI_MODEL`, the rates and the motion-tool builder are referenced
throughout `conversation.py` and `system.py`, and moving them would touch far
more than this seam is worth. They are imported inside the methods rather than
at module scope, because `conversation` imports this package -- at module
scope it is a circular import, and at call time it is not.

**Gemini 3.8 Live (2026-10).** Google now lists `gemini-3.8-live` as the stable
Live model and calls 3.1 "legacy". Both are offered; 3.1 keeps the settings it
was debugged with apart from the trailing silence below, which it turned out
to need as well. 3.8 needs two things, both measured against the
live API on 2026-10-04 with archive/bench_input_he.wav
(see `_GEMINI_38_NOTES` below for the numbers):

  * **Tool declarations must say BLOCKING.** 3.8 made NON_BLOCKING the default.
    With the default, the model called play_emotion and ended the turn without
    a word -- the mute-turn failure of 2026-07-26, back by a different door.
    With BLOCKING it gestures and talks, and the SILENT scheduling the turn
    loop uses after audio has started is honoured.
  * **The turn needs >= 2 s of trailing silence** (3.1 too, as of
    2026-10-04 -- see the notes). 3.8 ignores
    `audio_stream_end` as an end-of-turn signal and waits for its own voice
    activity detector to see the speaker stop. A recording that ends 0.8 s
    after the last word (our VAD hangover) sat unanswered for 25 s; 1.5 s also
    failed; 2 s and 3 s answered every time, ~3 s to first audio. The silence
    is appended to the blob, which is sent in one burst, so it costs no
    wall-clock time. 3 s is used for margin.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .base import SpeechProvider

log = logging.getLogger("reachy.provider.gemini")

GEMINI_38 = "gemini-3.8-live"
GEMINI_31 = "gemini-3.1-flash-live-preview"

# Per-model settings. A model not listed gets the 3.1 behaviour, which is the
# one that ran in production.
_MODEL_SETTINGS = {
    GEMINI_38: {"blocking_tools": True, "tail_pad_s": 3.0},
    # 3.1 gets the same padding: measured the same day, it too left a turn
    # ending 0.8 s after the last word unanswered (and then answered it late,
    # merged into the next turn), and answered 4/4 with 3 s. Its tools stay
    # on the SDK default it was debugged with.
    GEMINI_31: {"blocking_tools": False, "tail_pad_s": 3.0},
}

_GEMINI_38_NOTES = """
2026-10-04, gemini-3.8-live, bench_input_he.wav (3.25 s) + 0.5 s lead-in:
  tail 0.8 s, blocking ............ ACTIVITY_START only, nothing in 25 s
  tail 0.8 s, paced in real time .. nothing in 25 s
  tail 1.5 s, blocking (x2) ....... nothing in 25 s
  tail 2.0 s, blocking (x2) ....... answered, first audio 2.8-3.0 s, Hebrew
  tail 3.0 s, blocking ............ answered, first audio 3.0 s, Hebrew
  tail 2.0 s, default (NON_BLOCK) . tool call, then turn_complete, no speech

Same day, gemini-3.1-flash-live-preview, same clip:
  tail 0.8 s ...................... nothing in 25 s (also in the full dry run:
                                    turn 1 unanswered, then answered inside
                                    turn 2, every later turn one behind)
  tail 3.0 s, default tools (x2) .. answered, first audio 3.4 s, Hebrew
  tail 3.0 s, blocking (x2) ....... answered, first audio 3.1-3.4 s, Hebrew
"""


class GeminiProvider(SpeechProvider):
    name = "gemini"
    display_name = "Gemini Live"
    default_model = GEMINI_38
    # The five compared for Hebrew in tools/benchmark_voices.py.
    voices = ("Aoede", "Kore", "Charon", "Puck", "Leda")
    default_voice = "Aoede"
    default_language = "he-IL"
    implemented = True

    @staticmethod
    def _settings(model: str) -> dict:
        return _MODEL_SETTINGS.get(model or "", _MODEL_SETTINGS[GEMINI_31])

    def make_client(self, credential) -> Any:
        from google import genai
        return genai.Client(api_key=credential.key)

    def build_config(self, *, system_prompt: str, voice: str,
                     language: str, tools: Optional[list] = None,
                     model: str = "") -> Any:
        from google.genai import types
        import conversation as conv_mod

        if tools is None:
            tools = (conv_mod.build_motion_tools()
                     if conv_mod.ENABLE_TOOL_CALLS else [])

        if self._settings(model)["blocking_tools"]:
            # Explicit BLOCKING, restoring the semantics the turn loop's tool
            # handling (and its SILENT-after-audio scheduling) was built on.
            for tool in tools:
                for decl in (getattr(tool, "function_declarations", None) or []):
                    decl.behavior = types.Behavior.BLOCKING

        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            system_instruction=system_prompt,
            speech_config=types.SpeechConfig(
                language_code=language or self.default_language,
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=voice or self.default_voice
                    )
                ),
            ),
            tools=tools,
        )

    def connect(self, client: Any, config: Any, model: Optional[str] = None):
        import conversation as conv_mod
        return client.aio.live.connect(
            model=model or conv_mod.GEMINI_MODEL, config=config)

    def turn_tail_pad_s(self, model: str = "") -> float:
        return float(self._settings(model)["tail_pad_s"])

    def supports_language(self, language: str) -> bool:
        # Hebrew is in the Live API's language table, and he-IL is in
        # production on this robot (GPT-LIVE-MIGRATION-PLAN.md 0).
        return True
