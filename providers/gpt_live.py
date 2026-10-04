#!/usr/bin/env python3
"""`gpt-live-1` -- the seat at the table, not yet the backend.

This file exists so the seam is honest about what is coming, and so a robot
launched with `--provider gpt_live` gets a sentence explaining itself instead
of an import error. It does not implement anything, on purpose.

**The gate.** `GPT-LIVE-MIGRATION-PLAN.md` 6 is explicit: *"Do not write
integration code before Phase 0 returns a Hebrew verdict."* That spike is one
day, needs no robot, and answers whether `gpt-live-1` speaks acceptable Hebrew
at all. OpenAI publishes no language list for it, and its twelve voices are
English and Brazilian Portuguese. The expected outcome in the plan is that
`gpt-live-1` becomes the **English** backend while Gemini keeps Hebrew -- in
which case most of what would have been written here is never needed.

**And it is not a drop-in even then.** It is full-duplex with no turn
boundaries and no turn-completion event, and its tools live in a separate
delegation backend rather than in the voice model (plan 1 and 4.1). Every
turn-shaped thing in `conversation.py` -- the drain loop, the watchdogs, the
end-of-turn sentinel, `turn_NNN.wav`, `timings.csv` -- loses its anchor. That
is the second half of the seam described in `base.py`, and it is the work this
placeholder is holding the door open for.

What it needs when the gate opens:
  * `wss://api.openai.com/v1/live/sessions`, `Authorization: Bearer <key>`
  * `session.start` with `audio.format.rate` 16000 if spike item 6 passed --
    which deletes the whole `resample_poly` chain on both paths
  * motion tools kept local rather than routed through delegation (plan 4.2)
  * an answer on echo, because the robot's internal microphone now sits inches
    from its own speaker and full duplex means it hears itself (plan 4.3)
"""

from __future__ import annotations

from typing import Any, Optional

from .base import ProviderUnavailable, SpeechProvider

_NOT_YET = (
    "gpt-live-1 is not built yet. GPT-LIVE-MIGRATION-PLAN.md 6 gates it behind "
    "a one-day Hebrew spike that has not been run -- OpenAI publishes no "
    "language list for it and all twelve of its voices are English or "
    "Brazilian Portuguese. Launch this robot with --provider gemini until that "
    "spike says otherwise."
)


class GptLiveProvider(SpeechProvider):
    name = "gpt_live"
    display_name = "GPT-Live-1"
    default_model = "gpt-live-1"
    # Ten English, two Brazilian Portuguese (GPT-LIVE-MIGRATION-PLAN.md 1).
    voices = ("quartz", "ripple", "vesper", "willow", "stone", "gleam",
              "meridian", "beacon", "delta", "cinder", "bossa", "tempo")
    default_voice = "quartz"
    default_language = "en-US"
    implemented = False

    def make_client(self, credential) -> Any:
        raise ProviderUnavailable(_NOT_YET)

    def build_config(self, *, system_prompt: str, voice: str,
                     language: str, tools: Optional[list] = None) -> Any:
        raise ProviderUnavailable(_NOT_YET)

    def connect(self, client: Any, config: Any, model: Optional[str] = None):
        raise ProviderUnavailable(_NOT_YET)

    def supports_language(self, language: str) -> bool:
        # Hebrew is the open question the spike exists to answer; until it
        # does, claiming support would be a guess with a demo attached.
        return str(language or "").lower().startswith(("en", "pt"))
