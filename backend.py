#!/usr/bin/env python3
"""Which brain, which language, which voice -- the dashboard's backend picker.

Three small controls by the Start Conversation button:

  * **Brain**: Gemini 3.8 Live, Gemini 3.1 Flash Live, or OpenAI GPT-Live-1
    (providers.BRAINS).
  * **Language**: Hebrew or English. Picks the base prompt and the language
    code. The persona overlay still layers on top, with its closing
    reminder in the same language.
  * **ElevenLabs voice**: off, or on with a voice from the account. On means
    the brain still thinks and the robot speaks with ElevenLabs v4.

Like the persona switch, a change applies to the *next* conversation, never
the one already talking, and it survives a restart (backend.json beside the
app) so a test day can be set up once.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import providers as providers_mod
from providers import elevenlabs_voice

log = logging.getLogger("reachy.backend")

SCRIPT_DIR = Path(__file__).parent
BACKEND_PATH = SCRIPT_DIR / "backend.json"

LANGUAGES = (
    {"id": "he", "label": "עברית"},
    {"id": "en", "label": "English"},
)


@dataclass
class BackendChoice:
    brain: str = providers_mod.DEFAULT_BRAIN
    language: str = "he"
    elevenlabs: bool = False
    el_voice_id: str = ""
    el_voice_name: str = ""
    el_model: str = elevenlabs_voice.DEFAULT_MODEL
    updated_at: str = ""


class BackendStore:
    def __init__(self, path: Path = BACKEND_PATH,
                 default_brain: str = "") -> None:
        self.path = path
        self._default_brain = default_brain or providers_mod.DEFAULT_BRAIN
        self.state = self._load()

    def _load(self) -> BackendChoice:
        choice = BackendChoice(brain=self._default_brain)
        if not self.path.is_file():
            return choice
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read %s (%s); using the defaults",
                        self.path.name, exc)
            return choice
        if not isinstance(data, dict):
            return choice
        for name in asdict(choice):
            if name in data:
                setattr(choice, name, data[name])
        # A stale file must not be able to stop the robot starting.
        try:
            providers_mod.brain(choice.brain)
        except Exception:
            log.warning("saved brain '%s' no longer exists; using %s",
                        choice.brain, self._default_brain)
            choice.brain = self._default_brain
        if choice.language not in {l["id"] for l in LANGUAGES}:
            choice.language = "he"
        if choice.el_model not in elevenlabs_voice.MODELS:
            choice.el_model = elevenlabs_voice.DEFAULT_MODEL
        choice.elevenlabs = bool(choice.elevenlabs)
        return choice

    def save(self) -> None:
        try:
            self.path.write_text(
                json.dumps(asdict(self.state), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.warning("could not save %s: %s", self.path.name, exc)

    def update(self, brain: Optional[str] = None,
               language: Optional[str] = None,
               elevenlabs: Optional[bool] = None,
               el_voice_id: Optional[str] = None,
               el_voice_name: Optional[str] = None,
               el_model: Optional[str] = None) -> BackendChoice:
        s = self.state
        if brain is not None:
            s.brain = providers_mod.brain(str(brain))["id"]   # raises if unknown
        if language is not None:
            language = str(language).strip().lower()
            if language not in {l["id"] for l in LANGUAGES}:
                raise ValueError("language must be one of: he, en")
            s.language = language
        if elevenlabs is not None:
            s.elevenlabs = bool(elevenlabs)
        if el_voice_id is not None:
            s.el_voice_id = str(el_voice_id).strip()
            s.el_voice_name = str(el_voice_name or "").strip()
        if el_model is not None:
            if el_model not in elevenlabs_voice.MODELS:
                raise ValueError("ElevenLabs model must be one of: "
                                 + ", ".join(elevenlabs_voice.MODELS))
            s.el_model = el_model
        s.updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self.save()
        log.info("backend: %s, %s, voice %s", s.brain, s.language,
                 "ElevenLabs {} ({})".format(s.el_model, s.el_voice_name or s.el_voice_id or "default")
                 if s.elevenlabs else "native")
        return s
