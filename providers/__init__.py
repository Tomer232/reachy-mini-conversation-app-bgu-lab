"""Speech backends, one per file.

    providers/base.py             the contract, and what it deliberately leaves out
    providers/gemini.py           Gemini Live (3.8 and the proven 3.1)
    providers/gpt_live.py         OpenAI gpt-live-1, adapted to the turn loop
    providers/elevenlabs_voice.py not a backend: an optional voice that replaces
                                  whichever backend is thinking

A *provider* is a vendor and its client. A *brain* is what the dashboard's
dropdown offers: a provider plus a model. Two Gemini brains share one provider
and one key, which is why the dropdown is a list of brains, not of providers.
"""

from __future__ import annotations

from .base import ProviderUnavailable, SpeechProvider
from .gemini import GeminiProvider
from .gpt_live import GptLiveProvider

_PROVIDERS = {
    GeminiProvider.name: GeminiProvider,
    GptLiveProvider.name: GptLiveProvider,
}

DEFAULT_PROVIDER = GeminiProvider.name

# What the dashboard dropdown offers, in the order it offers it. `id` is what
# the browser sends back and what backend.json stores.
BRAINS = (
    {"id": "gemini-3.8", "provider": "gemini", "model": "gemini-3.8-live",
     "label": "Gemini 3.8 Live"},
    {"id": "gemini-3.1", "provider": "gemini",
     "model": "gemini-3.1-flash-live-preview",
     "label": "Gemini 3.1 Flash Live (previous)"},
    {"id": "gpt-live-1", "provider": "gpt_live", "model": "gpt-live-1",
     "label": "OpenAI GPT-Live-1"},
)
DEFAULT_BRAIN = "gemini-3.8"


def brain(brain_id: str = "") -> dict:
    """One dropdown entry by id. Unknown ids name the alternatives."""
    key = (brain_id or DEFAULT_BRAIN).strip().lower()
    for entry in BRAINS:
        if entry["id"] == key:
            return dict(entry)
    raise ProviderUnavailable(
        "'{}' is not a brain this app has (it has {})".format(
            brain_id, ", ".join(b["id"] for b in BRAINS)))


def get(name: str = "") -> SpeechProvider:
    """One provider instance by name. Unknown names name the alternatives."""
    key = (name or DEFAULT_PROVIDER).strip().lower()
    cls = _PROVIDERS.get(key)
    if cls is None:
        raise ProviderUnavailable(
            "'{}' is not a provider this app has (it has {})".format(
                name, ", ".join(sorted(_PROVIDERS))))
    return cls()


__all__ = ["BRAINS", "DEFAULT_BRAIN", "DEFAULT_PROVIDER", "ProviderUnavailable",
           "SpeechProvider", "brain", "get"]
