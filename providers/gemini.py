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
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .base import SpeechProvider

log = logging.getLogger("reachy.provider.gemini")


class GeminiProvider(SpeechProvider):
    name = "gemini"
    display_name = "Gemini Live"
    default_model = "gemini-3.1-flash-live-preview"
    # The five compared for Hebrew in tools/benchmark_voices.py.
    voices = ("Aoede", "Kore", "Charon", "Puck", "Leda")
    default_voice = "Aoede"
    default_language = "he-IL"
    implemented = True

    def make_client(self, credential) -> Any:
        from google import genai
        return genai.Client(api_key=credential.key)

    def build_config(self, *, system_prompt: str, voice: str,
                     language: str, tools: Optional[list] = None) -> Any:
        from google.genai import types
        import conversation as conv_mod

        if tools is None:
            tools = (conv_mod.build_motion_tools()
                     if conv_mod.ENABLE_TOOL_CALLS else [])

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

    def supports_language(self, language: str) -> bool:
        # 97 BCP-47 languages, and he-IL is in production on this robot
        # (GPT-LIVE-MIGRATION-PLAN.md 0).
        return True
