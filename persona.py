#!/usr/bin/env python3
"""The persona switch -- one robot's character, changeable from its dashboard.

Ten robots that all behave identically are ten copies of one demo. The point
of the switch is that a participant can make *their* Reachy a cowboy, or a
robot that only tells jokes, without a terminal and without anyone's help.

**Two states, and off is the default.** Off means the base persona, exactly as
it has always been -- a robot that was nudged and left alone is still itself.
On opens the panel and the robot takes the character typed into it. Flicking
the switch back off *is* the reset; nothing else has to be clicked, which is
why the typed text survives being switched off. Somebody who turns their
persona off to hear the plain robot for a minute gets their cowboy back.

**The overlay is layered on the base prompt, never a replacement.** A
participant cannot delete the instructions that make the robot work -- Hebrew,
short answers, one motion tool per reply. Those go in before the overlay
*and* are restated after it, because a model weights the end of its
instructions as well as the start, and "talk only in rhyme" is exactly the
kind of overlay that quietly takes a robot out of Hebrew.

**Voice belongs to the persona too**, since these robots are used by voice
first. The list of voices is not free-form: it depends on which provider that
robot's key belongs to, so `voices_for()` takes the provider. Gemini's five
are the ones actually benchmarked for Hebrew here (tools/benchmark_voices.py);
`gpt-live-1`'s twelve are English and Brazilian Portuguese only, which is why
a Hebrew robot on that provider is an open question and not a setting.

A persona survives a restart (it is written to persona.json next to the app),
so a demo day can be set up in the morning. The dashboard always shows what is
actually running, so a persona can never outlive the person who set it
unnoticed.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("reachy.persona")

SCRIPT_DIR = Path(__file__).parent
PERSONA_PATH = SCRIPT_DIR / "persona.json"
PRESETS_PATH = SCRIPT_DIR / "presets.json"

# A participant typing into a box on a phone is not an adversary, but the box
# still needs a bound: an overlay long enough to push the base prompt out of
# the model's attention defeats the layering this module exists to guarantee.
MAX_OVERLAY_CHARS = 1500

# Restated after the overlay. The base prompt already says all of this; saying
# it again *last* is what stops "answer only in English pirate slang" from
# quietly winning. Deliberately short -- a long guard is a long instruction to
# argue with.
INVARIANTS_HE = (
    "חשוב, גם עם האופי שלמעלה: ענה תמיד בעברית, "
    "שמור על תשובות קצרות של משפט או שניים, "
    "והשתמש בכלי תנועה אחד לכל היותר בכל תשובה."
)

# Voices, per provider.
#
# Gemini: the five that tools/benchmark_voices.py actually compared for Hebrew
# on gemini-3.1-flash-live-preview. Aoede is the incumbent and the default.
#
# gpt-live-1: the twelve OpenAI publishes (GPT-LIVE-MIGRATION-PLAN.md 1). Ten
# English, two Brazilian Portuguese, and **no documented Hebrew support at
# all** -- listed here so the seam is complete, not because any of them is
# known to work for this lab's Hebrew. That is what the Phase 0 spike decides.
VOICES = {
    "gemini": ["Aoede", "Kore", "Charon", "Puck", "Leda"],
    "gpt_live": ["quartz", "ripple", "vesper", "willow", "stone", "gleam",
                 "meridian", "beacon", "delta", "cinder", "bossa", "tempo"],
}

DEFAULT_VOICE = {"gemini": "Aoede", "gpt_live": "quartz"}

# Starting points, so nobody faces an empty box. Editable in presets.json
# without touching code -- these are written out on first run if the file is
# absent. Each overlay is written the way a participant would want it read:
# a character, not a rule list.
DEFAULT_PRESETS = [
    {
        "id": "cowboy",
        "label": "קאובוי",
        "overlay": "אתה קאובוי מהמערב הפרוע. דבר בביטחון ובחום, "
                   "השתמש בסלנג של קאובוי, וקרא למשתמש שותף.",
    },
    {
        "id": "jokes",
        "label": "בדיחות בלבד",
        "overlay": "אתה קומיקאי. ענה תמיד עם בדיחה או משחק מילים, "
                   "גם כשהשאלה רצינית.",
    },
    {
        "id": "scientist",
        "label": "מדען",
        "overlay": "אתה מדען סקרן. הסבר כל דבר בהתלהבות ובפשטות, "
                   "ותמיד תוסיף עובדה מפתיעה אחת.",
    },
    {
        "id": "pirate",
        "label": "פיראט",
        "overlay": "אתה פיראט זקן ועליז. דבר כמו יורד ים, "
                   "וספר סיפורים קצרים על הרפתקאות בים.",
    },
    {
        "id": "shy",
        "label": "ביישן",
        "overlay": "אתה רובוט ביישן ומנומס. דבר בשקט, בהיסוס קל, "
                   "ותתרגש כשמחמיאים לך.",
    },
]


def _clean(text: str) -> str:
    """Strip control characters and normalise, leaving the words alone.

    Hebrew arrives from a phone keyboard with directional marks and the
    occasional stray control character; neither belongs in a system prompt,
    and both survive a naive strip().
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFC", str(text))
    text = "".join(ch for ch in text
                   if ch in "\n\t" or not unicodedata.category(ch).startswith("C"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


@dataclass
class PersonaState:
    """What the switch is currently set to. `enabled` is the switch itself."""

    enabled: bool = False
    overlay: str = ""
    preset_id: str = ""
    voice: str = ""
    updated_at: str = ""

    def public(self, provider: str = "gemini") -> dict:
        """What the dashboard renders. Includes the voice actually in force,
        so the panel never shows a voice the robot is not using."""
        return {
            "enabled": self.enabled,
            "overlay": self.overlay,
            "preset_id": self.preset_id,
            "voice": self.voice,
            "updated_at": self.updated_at,
            "active_voice": self.effective_voice(DEFAULT_VOICE.get(provider, "")),
            "max_chars": MAX_OVERLAY_CHARS,
        }

    # ----- what the model actually receives -----

    def effective_prompt(self, base: str) -> str:
        """base, when the switch is off. base + overlay + invariants when on.

        The order is the whole design: the working instructions cannot be
        removed, only added to.
        """
        overlay = _clean(self.overlay)
        if not self.enabled or not overlay:
            return base
        return "{}\n\n{}\n\n{}".format(base.strip(), overlay, INVARIANTS_HE)

    def effective_voice(self, default: str) -> str:
        if not self.enabled or not self.voice:
            return default
        return self.voice

    @property
    def is_active(self) -> bool:
        """On *and* actually saying something. The switch alone changes
        nothing if the box behind it is empty."""
        return bool(self.enabled and (_clean(self.overlay) or self.voice))

    def summary(self) -> str:
        """One line for a log or a header."""
        if not self.is_active:
            return "base"
        if self.preset_id:
            return "preset:{}".format(self.preset_id)
        return "custom ({} chars)".format(len(_clean(self.overlay)))


class PersonaStore:
    """Loads, validates and persists one robot's persona."""

    def __init__(self, path: Path = PERSONA_PATH,
                 presets_path: Path = PRESETS_PATH,
                 provider: str = "gemini") -> None:
        self.path = path
        self.presets_path = presets_path
        self.provider = provider
        self.state = self._load()

    # ----- presets -----

    def presets(self) -> list:
        """The curated starting points. Falls back to the built-ins rather
        than to an empty list: an empty preset menu looks like a bug to a
        participant, and there is nothing to be gained from showing one."""
        if self.presets_path.is_file():
            try:
                data = json.loads(self.presets_path.read_text(encoding="utf-8"))
                entries = data.get("presets") if isinstance(data, dict) else data
                if isinstance(entries, list) and entries:
                    return [e for e in entries
                            if isinstance(e, dict) and e.get("id")]
            except Exception as exc:  # noqa: BLE001
                log.warning("could not read %s (%s); using the built-in presets",
                            self.presets_path.name, exc)
        return list(DEFAULT_PRESETS)

    def preset(self, preset_id: str) -> Optional[dict]:
        for entry in self.presets():
            if str(entry.get("id")) == preset_id:
                return entry
        return None

    def voices(self) -> list:
        return list(VOICES.get(self.provider, []))

    # ----- persistence -----

    def _load(self) -> PersonaState:
        if not self.path.is_file():
            return PersonaState()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            # A corrupt persona file must not stop the robot starting. Base
            # persona is always a safe thing to fall back to.
            log.warning("could not read %s (%s); starting on the base persona",
                        self.path.name, exc)
            return PersonaState()
        if not isinstance(data, dict):
            return PersonaState()
        state = PersonaState(
            enabled=bool(data.get("enabled")),
            overlay=_clean(data.get("overlay") or "")[:MAX_OVERLAY_CHARS],
            preset_id=str(data.get("preset_id") or ""),
            voice=str(data.get("voice") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )
        if state.voice and state.voice not in self.voices():
            # The provider changed under a saved persona -- an Aoede persona on
            # a gpt-live robot. Drop the voice, keep the character.
            log.warning("saved voice '%s' is not a %s voice; falling back to "
                        "the default voice and keeping the rest of the persona",
                        state.voice, self.provider)
            state.voice = ""
        if state.is_active:
            log.info("persona is ON at startup: %s", state.summary())
        return state

    def save(self) -> None:
        payload = {
            "enabled": self.state.enabled,
            "overlay": self.state.overlay,
            "preset_id": self.state.preset_id,
            "voice": self.state.voice,
            "updated_at": self.state.updated_at,
        }
        try:
            self.path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.warning("could not save the persona to %s: %s",
                        self.path.name, exc)

    # ----- the switch -----

    def update(self, enabled: Optional[bool] = None,
               overlay: Optional[str] = None,
               preset_id: Optional[str] = None,
               voice: Optional[str] = None) -> PersonaState:
        """Apply a change from the dashboard. Only the fields given move.

        Selecting a preset fills the box with that preset's text, because the
        panel is one box: a preset is a *starting point* a participant can then
        edit, not a separate mode with its own hidden state.
        """
        state = self.state

        if preset_id is not None:
            preset_id = str(preset_id).strip()
            if preset_id:
                entry = self.preset(preset_id)
                if entry is None:
                    raise ValueError(
                        "there is no preset called '{}'".format(preset_id))
                state.preset_id = preset_id
                if overlay is None:
                    overlay = str(entry.get("overlay") or "")
            else:
                state.preset_id = ""

        if overlay is not None:
            cleaned = _clean(overlay)
            if len(cleaned) > MAX_OVERLAY_CHARS:
                raise ValueError(
                    "that persona is {} characters; the limit is {}".format(
                        len(cleaned), MAX_OVERLAY_CHARS))
            # Typing over a preset makes it custom. Claiming it is still the
            # preset would misreport what the robot is doing.
            if preset_id is None and state.preset_id:
                entry = self.preset(state.preset_id)
                if entry is not None and _clean(entry.get("overlay") or "") != cleaned:
                    state.preset_id = ""
            state.overlay = cleaned

        if voice is not None:
            voice = str(voice).strip()
            if voice and voice not in self.voices():
                raise ValueError(
                    "'{}' is not a voice {} offers ({})".format(
                        voice, self.provider, ", ".join(self.voices())))
            state.voice = voice

        if enabled is not None:
            state.enabled = bool(enabled)

        state.updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self.save()
        log.info("persona switch: %s (%s)",
                 "ON" if state.enabled else "OFF", state.summary())
        return state

    def reset(self) -> PersonaState:
        """Back to base, and forget the text as well.

        Distinct from switching off: off keeps the words for later, reset
        clears the panel. The dashboard offers both because they answer
        different questions -- "let me hear the plain robot" and "this is not
        mine, clear it".
        """
        self.state = PersonaState(
            updated_at=datetime.now().astimezone().isoformat(timespec="seconds"))
        self.save()
        log.info("persona reset to base")
        return self.state

    # ----- what the session builder asks for -----

    def prompt_for(self, base: str) -> str:
        return self.state.effective_prompt(base)

    def voice_for(self, default: str) -> str:
        return self.state.effective_voice(default)


def write_default_presets(path: Path = PRESETS_PATH) -> bool:
    """Materialise the built-in presets so they can be edited on disk.
    Returns True if a file was written. Never overwrites an existing one."""
    if path.is_file():
        return False
    try:
        path.write_text(
            json.dumps({"presets": DEFAULT_PRESETS}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("could not write %s: %s", path.name, exc)
        return False
