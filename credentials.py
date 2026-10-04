#!/usr/bin/env python3
"""Which API key does *this* robot talk through?

One robot meant one key, and `get_api_key()` in conversation.py encoded that:
one env var, one file, one hardcoded path. A lab with ten robots and three
keys needs the other shape -- an arbitrary many-robots-to-one-key map, decided
by a human rather than by whatever the environment happens to hold.

The map itself is not here. The hub owns it (docs/HUB-INTERFACE.md): you draw
"robots 1 and 2 on key A, 3 and 4 on key B" once, in one place, and the hub
hands each robot its provider and key id at launch. This module is the
receiving end -- it resolves what was passed against a deployed registry and
hands back one credential.

    keys.json
    {
      "keys": [
        {"id": "gemini-lab-1", "provider": "gemini",   "label": "Lab Gemini #1", "key": "..."},
        {"id": "openai-demo",  "provider": "gpt_live", "label": "OpenAI demo",   "key": "..."}
      ]
    }

A key may also be given as the *name of an environment variable* holding it
("key_env": "GEMINI_API_KEY_2"), which is how a registry gets deployed to ten
robots without ten copies of a secret sitting in a file.

**Keys never reach the dashboard.** Credential.public() is what the UI and the
event log get: provider, id, label, and a four-character tail so you can tell
two keys apart in a screenshot without either of them being in it.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("reachy.credentials")

SCRIPT_DIR = Path(__file__).parent
KEYS_PATH = SCRIPT_DIR / "keys.json"

GEMINI = "gemini"
GPT_LIVE = "gpt_live"
# Not a speech backend: the optional voice that can replace either backend's
# own (providers/elevenlabs_voice.py). It still needs a key, and the key lives
# wherever the other two live, so it is resolved the same way.
ELEVENLABS = "elevenlabs"
PROVIDERS = (GEMINI, GPT_LIVE, ELEVENLABS)

# The env var each provider falls back to when no registry entry applies.
_ENV_FALLBACK = {GEMINI: "GEMINI_API_KEY", GPT_LIVE: "OPENAI_API_KEY",
                 ELEVENLABS: "ELEVENLABS_API_KEY"}

# Set by robot-hub at launch; see resolve().
HUB_KEY_ENV = "REACHY_HUB_KEY"
HUB_KEY_ID_ENV = "REACHY_HUB_KEY_ID"
HUB_KEY_LABEL_ENV = "REACHY_HUB_KEY_LABEL"


class NoCredential(RuntimeError):
    """No usable key for the requested provider. The message is written to be
    read by whoever is standing next to the robot, not by a developer."""


@dataclass
class Credential:
    provider: str
    key: str
    key_id: str = ""
    label: str = ""
    source: str = ""

    @property
    def tail(self) -> str:
        """The last four characters. Enough to tell two keys apart in a
        screenshot; not enough to be one."""
        return self.key[-4:] if len(self.key) >= 4 else "?"

    def public(self) -> dict[str, Any]:
        """Everything about this credential that is safe to broadcast."""
        return {
            "provider": self.provider,
            "key_id": self.key_id,
            "label": self.label or self.key_id or self.provider,
            "key_tail": self.tail,
            "source": self.source,
        }


def _load_registry(path: Path = KEYS_PATH) -> list:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read %s (%s); falling back to the environment",
                    path.name, exc)
        return []
    entries = data.get("keys") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        log.warning("%s has no 'keys' list; falling back to the environment",
                    path.name)
        return []
    return [e for e in entries if isinstance(e, dict)]


def _entry_key(entry: dict) -> str:
    """The secret for one registry entry: inline, or out of the environment."""
    env_name = entry.get("key_env")
    if env_name:
        value = os.environ.get(str(env_name), "")
        if value:
            return value.strip()
        log.warning("key '%s' points at %s, which is not set on this robot",
                    entry.get("id", "?"), env_name)
        return ""
    return str(entry.get("key") or "").strip()


def list_keys(path: Path = KEYS_PATH) -> list:
    """The registry as the dashboard may see it -- no secrets, and entries
    whose key is missing on this robot are marked rather than hidden, because
    a key that silently is not there is how a demo dies at the worst moment."""
    out = []
    for entry in _load_registry(path):
        key = _entry_key(entry)
        out.append({
            "id": str(entry.get("id") or ""),
            "provider": str(entry.get("provider") or GEMINI),
            "label": str(entry.get("label") or entry.get("id") or ""),
            "available": bool(key),
            "key_tail": key[-4:] if len(key) >= 4 else "",
        })
    return out


def _legacy_gemini_key() -> tuple:
    """The pre-fleet resolution, preserved exactly. Returns (key, source).

    Kept because every existing checkout and both deploy paths rely on it, and
    a fleet feature must not break the single robot that works today.
    """
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key.strip(), "env GEMINI_API_KEY"
    local_key = SCRIPT_DIR / ".gemini_key"
    if local_key.exists():
        return local_key.read_text(encoding="utf-8").strip(), ".gemini_key"
    tomer_path = SCRIPT_DIR.resolve().parent / "reachy-mini llm gemini token.txt"
    if tomer_path.exists():
        return tomer_path.read_text(encoding="utf-8").strip(), "local token file"
    return "", ""


def resolve(provider: Optional[str] = None,
            key_id: Optional[str] = None,
            literal_key: Optional[str] = None,
            path: Path = KEYS_PATH,
            use_hub: bool = True) -> Credential:
    """Settle this instance's credential.

    `key_id` names a registry entry (and then decides the provider, unless one
    was given explicitly and disagrees -- which is an error worth raising, not
    papering over). Without one, the registry's first entry for the provider
    wins, then the environment.

    `use_hub=False` skips the hub's key. The hub assigns one key for the
    provider the robot was *launched* on; once the dashboard can switch
    backends per conversation, handing that same key to a different provider
    would send a Gemini key to OpenAI. The caller knows which provider was
    launched, so the caller decides.
    """
    provider = (provider or GEMINI).strip().lower()
    if provider not in PROVIDERS:
        raise NoCredential(
            "'{}' is not a provider this app knows about (it has {})".format(
                provider, ", ".join(PROVIDERS)))

    if literal_key:
        return Credential(provider=provider, key=literal_key.strip(),
                          key_id="(passed in)", label="passed at launch",
                          source="--api-key")

    # The hub owns the keys (Tomer, 2026-09-24): it pipes the assigned one into
    # this process's environment at launch, so no copy lives on the robot's
    # disk and none appears in a process list. It outranks everything stored.
    hub_key = os.environ.get(HUB_KEY_ENV, "").strip() if use_hub else ""
    if hub_key:
        return Credential(provider=provider, key=hub_key,
                          key_id=os.environ.get(HUB_KEY_ID_ENV, ""),
                          label=os.environ.get(HUB_KEY_LABEL_ENV, "") or "from the hub",
                          source="hub")

    registry = _load_registry(path)

    if key_id:
        for entry in registry:
            if str(entry.get("id")) != key_id:
                continue
            entry_provider = str(entry.get("provider") or provider).lower()
            if entry_provider != provider:
                raise NoCredential(
                    "key '{}' is a {} key but this robot was launched for {} "
                    "-- the hub's assignment and the --provider flag "
                    "disagree".format(key_id, entry_provider, provider))
            key = _entry_key(entry)
            if not key:
                raise NoCredential(
                    "key '{}' is in {} but its secret is not on this robot "
                    "(check its key_env)".format(key_id, path.name))
            return Credential(provider=provider, key=key, key_id=key_id,
                              label=str(entry.get("label") or key_id),
                              source=path.name)
        raise NoCredential(
            "the hub assigned key '{}' but no key by that id is in {} on this "
            "robot -- redeploy the key registry".format(key_id, path.name))

    for entry in registry:
        if str(entry.get("provider") or GEMINI).lower() != provider:
            continue
        key = _entry_key(entry)
        if key:
            eid = str(entry.get("id") or "")
            return Credential(provider=provider, key=key, key_id=eid,
                              label=str(entry.get("label") or eid),
                              source="{} (first {} key)".format(path.name, provider))

    if provider == GEMINI:
        key, source = _legacy_gemini_key()
        if key:
            return Credential(provider=provider, key=key, key_id="",
                              label="environment", source=source)

    env_name = _ENV_FALLBACK[provider]
    key = os.environ.get(env_name, "").strip()
    if key:
        return Credential(provider=provider, key=key, key_id="",
                          label="environment", source="env " + env_name)

    raise NoCredential(
        "no {} key on this robot. Give it one of: a keys.json entry, the {} "
        "environment variable, or --api-key at launch".format(provider, env_name))
