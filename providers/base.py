#!/usr/bin/env python3
"""The seam between the turn loop and whoever is actually doing the talking.

`GPT-LIVE-MIGRATION-PLAN.md` 7.1 asks for this and says to build it *even if
`gpt-live-1` is never shipped*, because it is what makes the decision
reversible. It also says the Gemini path must be **moved, not rewritten** --
that path is the one thing here proven in Hebrew on real hardware, and it is
not worth risking for tidiness.

**What this seam covers today, and what it deliberately does not.**

It covers the session-shaped things: which client, which model, which voice,
which language, how a session config is built, how a session is opened. Those
are where a provider choice actually lives, they are what the persona switch
and the key registry need to reach, and they can be changed with no robot
present.

It does **not** yet cover the event stream. The plan's full interface -- the
provider-neutral `AudioChunk` / `UserTranscript` / `ToolCall` / `Interrupted`
iterator -- means restructuring a 2,328-line turn loop that has been debugged
against live hardware over months, and there is no robot available to prove
the result identical. So the turn loop still speaks to the Gemini session
object directly. `gpt-live-1` is what forces that second half, because it is
full-duplex and has no turn boundaries at all (plan 4.1) -- and by then the
Phase 0 spike will have said whether it is worth building.

Naming the limit is the point. A seam that claims to abstract something it
does not is worse than no seam, because the next person builds on the claim.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional


class ProviderUnavailable(RuntimeError):
    """This provider cannot run here, with a sentence saying why."""


class SpeechProvider(ABC):
    """One speech backend, as far as the rest of the app is concerned."""

    #: the id used on the command line and in keys.json
    name: str = ""
    #: what a human is shown
    display_name: str = ""
    #: model id this provider talks to by default
    default_model: str = ""
    #: voices offered, in the order a menu should show them
    voices: tuple = ()
    default_voice: str = ""
    default_language: str = ""

    #: False while a backend exists as a seam but not as an implementation.
    #: The dashboard uses this to explain itself rather than fail obscurely.
    implemented: bool = True

    # ----- resources -----

    @abstractmethod
    def make_client(self, credential) -> Any:
        """Build the SDK client for one `credentials.Credential`."""

    # ----- one session -----

    @abstractmethod
    def build_config(self, *, system_prompt: str, voice: str,
                     language: str, tools: Optional[list] = None) -> Any:
        """The provider-native session config.

        `system_prompt` arrives already layered by the persona switch; a
        provider must pass it through untouched.
        """

    @abstractmethod
    def connect(self, client: Any, config: Any, model: Optional[str] = None):
        """An async context manager yielding a live session."""

    # ----- telemetry -----

    def flags(self, *, model: str = "", voice: str = "",
              language: str = "") -> dict:
        """Provider-shaped entries for `summary.json`'s config block, so a run
        can never be read back without knowing what produced it."""
        return {
            "provider": self.name,
            "model": model or self.default_model,
            "voice": voice or self.default_voice,
            "language": language or self.default_language,
        }

    def supports_language(self, language: str) -> bool:
        """Whether this provider is *known* to handle a language. Deliberately
        conservative: unknown means no, because the cost of finding out during
        a demo is a robot that answers a Hebrew question in English."""
        return True
