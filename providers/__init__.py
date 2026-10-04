"""Speech backends, one per file, chosen at launch by `--provider`.

    providers/base.py       the contract, and what it deliberately leaves out
    providers/gemini.py     the working path, moved not rewritten
    providers/gpt_live.py   the seat at the table; gated on the Hebrew spike
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


def get(name: str = "") -> SpeechProvider:
    """One provider instance by name. Unknown names name the alternatives."""
    key = (name or DEFAULT_PROVIDER).strip().lower()
    cls = _PROVIDERS.get(key)
    if cls is None:
        raise ProviderUnavailable(
            "'{}' is not a provider this app has (it has {})".format(
                name, ", ".join(sorted(_PROVIDERS))))
    return cls()


def available() -> list:
    """Every provider, with whether it is actually built. The dashboard shows
    the unbuilt ones rather than hiding them, so the roadmap is visible where
    the decision is made."""
    out = []
    for key in sorted(_PROVIDERS):
        p = _PROVIDERS[key]()
        out.append({
            "name": p.name,
            "display_name": p.display_name,
            "implemented": p.implemented,
            "voices": list(p.voices),
            "default_voice": p.default_voice,
        })
    return out


__all__ = ["SpeechProvider", "ProviderUnavailable", "GeminiProvider",
           "GptLiveProvider", "get", "available", "DEFAULT_PROVIDER"]
