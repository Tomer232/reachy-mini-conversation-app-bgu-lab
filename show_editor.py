"""Edit the show script from the operator board.

Until now the cue script was a file you edited by hand and rebuilt from a
terminal:

    notepad show\\cues.json
    python tools\\build_show.py

That is fine at a desk and useless the evening before a talk, when the boss
wants a line reworded and nobody wants to be at a keyboard editing JSON. This
module puts the same operations behind the dashboard: rewrite a line, add a
cue, delete a cue — and re-synthesise only what changed.

Deliberately the *same* mechanics as `tools/build_show.py`, not a parallel
implementation: the same `synthesise()`, the same `text_hash()`, the same
manifest format, the same `docs/SHOW_SCRIPT.md` regeneration. Editing from the
browser and rebuilding from the terminal stay interchangeable, and a cue built
either way is identical on disk.

Every write goes through `_save_cues`, which backs up the previous file first.
The show script is the one artifact here with no other copy — losing it the
night before the lecture would be unrecoverable — so the cost of a backup per
edit is worth paying.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sys
import threading
import unicodedata
from datetime import datetime
from pathlib import Path

import soundfile as sf

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "tools"))

SHOW_DIR = SCRIPT_DIR / "show"
CUES_PATH = SHOW_DIR / "cues.json"
AUDIO_DIR = SHOW_DIR / "audio"
MANIFEST_PATH = SHOW_DIR / "manifest.json"
BACKUP_DIR = SHOW_DIR / "backups"

log = logging.getLogger("reachy.show.editor")

# Serialises edits. Two operators on two browser tabs would otherwise
# read-modify-write cues.json over each other, and the loser's line vanishes
# with no error anywhere.
_EDIT_LOCK = threading.RLock()

MAX_TEXT_CHARS = 600
MAX_LABEL_CHARS = 80


class ShowEditError(Exception):
    """Anything the operator can fix by changing what they typed."""


# ----- file I/O ------------------------------------------------------

def load_cues() -> dict:
    if not CUES_PATH.exists():
        raise ShowEditError(f"no cue file at {CUES_PATH}")
    return json.loads(CUES_PATH.read_text(encoding="utf-8"))


def _save_cues(data: dict) -> None:
    """Back up, then write atomically."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if CUES_PATH.exists():
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        shutil.copy2(CUES_PATH, BACKUP_DIR / f"cues_{stamp}.json")
        # Keep the last 30. Enough to undo a bad evening, not enough to grow
        # without bound on the robot's SD card.
        backups = sorted(BACKUP_DIR.glob("cues_*.json"))
        for old in backups[:-30]:
            old.unlink(missing_ok=True)

    tmp = CUES_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(CUES_PATH)


def _load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        try:
            m = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            m.setdefault("cues", {})
            return m
        except Exception:
            log.exception("manifest unreadable; starting a fresh one")
    return {"cues": {}}


def _save_manifest(manifest: dict) -> None:
    tmp = MANIFEST_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(MANIFEST_PATH)


# ----- lookups -------------------------------------------------------

def _iter_cues(data: dict):
    for section in data.get("sections", []):
        for cue in section.get("cues", []):
            yield section, cue


def _find(data: dict, cue_id: str):
    for section, cue in _iter_cues(data):
        if cue.get("id") == cue_id:
            return section, cue
    raise ShowEditError(f"no cue with id {cue_id!r}")


def _slug(label: str, existing: set[str]) -> str:
    """A filesystem-safe, unique cue id derived from the label.

    The id becomes a WAV filename, so it has to survive a filesystem and an
    HTTP path. Hebrew labels are the norm here and transliterate to nothing
    useful, so those fall back to a counter rather than producing an empty or
    mojibake id.
    """
    ascii_label = (unicodedata.normalize("NFKD", label or "")
                   .encode("ascii", "ignore").decode("ascii"))
    base = re.sub(r"[^a-zA-Z0-9]+", "_", ascii_label).strip("_").lower()[:32]
    if not base:
        base = "cue"
    candidate, n = base, 2
    while candidate in existing:
        candidate = f"{base}_{n}"
        n += 1
    return candidate


# ----- validation ----------------------------------------------------

def _validate_motion(motion: dict | None) -> dict | None:
    """Check a motion against the catalogs the robot actually has.

    Rejecting here rather than at showtime is the whole point: an unknown name
    fails silently on the robot (it logs and keeps the current move), so a
    typo would look like a cue that simply does not move — mid-lecture, with no
    obvious cause.
    """
    if not motion:
        return None
    import conversation as conv

    kind = (motion.get("type") or "").strip()
    if kind == "":
        return None
    if kind == "emotion":
        name = (motion.get("name") or "").strip()
        if conv.EMOTION_NAMES and name not in conv.EMOTION_NAMES:
            raise ShowEditError(f"unknown emotion {name!r}")
        return {"type": "emotion", "name": name}
    if kind == "dance":
        name = (motion.get("name") or "").strip()
        if conv.DANCE_NAMES and name not in conv.DANCE_NAMES:
            raise ShowEditError(f"unknown dance {name!r}")
        return {"type": "dance", "name": name}
    if kind == "head":
        d = (motion.get("direction") or "").strip()
        if d not in conv.HEAD_DIRECTIONS:
            raise ShowEditError(
                f"unknown head direction {d!r} "
                f"(use one of: {', '.join(conv.HEAD_DIRECTIONS)})")
        return {"type": "head", "direction": d}
    raise ShowEditError(f"unknown motion type {kind!r}")


def _validate_hotkey(data: dict, hotkey: str | None, own_id: str | None) -> str | None:
    if hotkey is None:
        return None
    hotkey = hotkey.strip()
    if hotkey == "":
        return None
    if len(hotkey) != 1:
        raise ShowEditError("hotkey must be a single character")
    for _s, cue in _iter_cues(data):
        if cue.get("id") != own_id and (cue.get("hotkey") or "").lower() == hotkey.lower():
            raise ShowEditError(
                f"hotkey {hotkey!r} is already used by {cue.get('label') or cue['id']!r}")
    return hotkey


def _validate_text(text: str | None) -> str | None:
    if text is None:
        return None
    text = text.strip()
    if text == "":
        return None            # motion-only cue
    if len(text) > MAX_TEXT_CHARS:
        raise ShowEditError(
            f"line is {len(text)} characters; keep it under {MAX_TEXT_CHARS} "
            f"(long lines make for long waits on stage)")
    return text


# ----- synthesis -----------------------------------------------------

def _synthesise_cue(data: dict, cue: dict, force: bool = False) -> dict:
    """Build this cue's WAV if its text changed. Returns a status dict.

    Reuses build_show's synthesise() and hash so a browser edit and a terminal
    rebuild produce byte-identical results and neither invalidates the other's
    cache.
    """
    import build_show                    # tools/ is on sys.path
    import conversation as conv
    from google import genai

    cid = cue["id"]
    text = cue.get("text")
    manifest = _load_manifest()
    wav_path = AUDIO_DIR / f"{cid}.wav"

    if not text:
        # Motion-only cue: drop any audio it used to have, or the board would
        # keep showing a stale line under a cue that no longer speaks.
        wav_path.unlink(missing_ok=True)
        manifest["cues"].pop(cid, None)
        _save_manifest(manifest)
        return {"cue_id": cid, "synthesised": False, "reason": "motion only"}

    voice = data.get("voice", "Aoede")
    style = data.get("style", "")
    model = data.get("tts_model", "gemini-3.1-flash-tts-preview")
    h = build_show.text_hash(text, voice, style)

    entry = manifest["cues"].get(cid)
    if not force and entry and entry.get("hash") == h and wav_path.exists():
        return {"cue_id": cid, "synthesised": False, "reason": "unchanged"}

    log.info("show edit: synthesising %s (%d chars)…", cid, len(text))
    client = genai.Client(api_key=conv.get_api_key())
    samples = build_show.synthesise(client, model, voice, style, text)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    sf.write(wav_path, samples, build_show.TTS_RATE, subtype="PCM_16")
    duration = len(samples) / build_show.TTS_RATE

    manifest["cues"][cid] = {"hash": h, "duration_s": round(duration, 2),
                            "file": f"audio/{cid}.wav", "text": text}
    manifest.setdefault("voice", voice)
    manifest.setdefault("model", model)
    _save_manifest(manifest)
    log.info("show edit: %s -> %.2fs", cid, duration)
    return {"cue_id": cid, "synthesised": True, "duration_s": round(duration, 2)}


def _rewrite_boss_script(data: dict) -> None:
    """Keep docs/SHOW_SCRIPT.md — what the boss reads from — in step with the
    cues. An edit that updates the robot but not the printed sheet is worse
    than no edit at all."""
    try:
        import build_show
        build_show.write_script(data, _load_manifest())
    except Exception:
        log.exception("could not regenerate SHOW_SCRIPT.md (cues were saved)")


# ----- operations ----------------------------------------------------

def update_cue(cue_id: str, fields: dict) -> dict:
    """Change an existing cue. Only the keys present in `fields` are touched."""
    with _EDIT_LOCK:
        data = load_cues()
        _section, cue = _find(data, cue_id)

        if "label" in fields:
            label = (fields["label"] or "").strip()
            if not label:
                raise ShowEditError("label cannot be empty")
            cue["label"] = label[:MAX_LABEL_CHARS]
        if "text" in fields:
            text = _validate_text(fields["text"])
            if text is None:
                cue.pop("text", None)
            else:
                cue["text"] = text
        if "boss_cue" in fields:
            bc = (fields["boss_cue"] or "").strip()
            if bc:
                cue["boss_cue"] = bc
            else:
                cue.pop("boss_cue", None)
        if "hotkey" in fields:
            hk = _validate_hotkey(data, fields["hotkey"], cue_id)
            if hk:
                cue["hotkey"] = hk
            else:
                cue.pop("hotkey", None)
        if "motion" in fields:
            motion = _validate_motion(fields["motion"])
            if motion:
                cue["motion"] = motion
            else:
                cue.pop("motion", None)

        if not cue.get("text") and not cue.get("motion"):
            raise ShowEditError(
                "a cue needs a line to speak, a motion, or both — "
                "this one would do nothing")

        _save_cues(data)
        result = _synthesise_cue(data, cue)
        _rewrite_boss_script(data)
        return result


def add_cue(section_id: str, fields: dict) -> dict:
    """Append a new cue to a section."""
    with _EDIT_LOCK:
        data = load_cues()
        section = next((s for s in data.get("sections", [])
                        if s.get("id") == section_id), None)
        if section is None:
            raise ShowEditError(f"no section {section_id!r}")

        label = (fields.get("label") or "").strip()
        if not label:
            raise ShowEditError("give the cue a label so you can find it")
        text = _validate_text(fields.get("text"))
        motion = _validate_motion(fields.get("motion"))
        if not text and not motion:
            raise ShowEditError("a cue needs a line to speak, a motion, or both")
        hotkey = _validate_hotkey(data, fields.get("hotkey"), None)

        existing = {c.get("id") for _s, c in _iter_cues(data)}
        cue = {"id": _slug(label, existing), "label": label[:MAX_LABEL_CHARS]}
        if hotkey:
            cue["hotkey"] = hotkey
        if text:
            cue["text"] = text
        if motion:
            cue["motion"] = motion
        bc = (fields.get("boss_cue") or "").strip()
        if bc:
            cue["boss_cue"] = bc

        section.setdefault("cues", []).append(cue)
        _save_cues(data)
        result = _synthesise_cue(data, cue)
        _rewrite_boss_script(data)
        result["cue_id"] = cue["id"]
        return result


def delete_cue(cue_id: str) -> dict:
    with _EDIT_LOCK:
        data = load_cues()
        section, cue = _find(data, cue_id)
        section["cues"] = [c for c in section["cues"] if c.get("id") != cue_id]
        _save_cues(data)

        (AUDIO_DIR / f"{cue_id}.wav").unlink(missing_ok=True)
        manifest = _load_manifest()
        manifest["cues"].pop(cue_id, None)
        _save_manifest(manifest)
        _rewrite_boss_script(data)
        log.info("show edit: deleted cue %s", cue_id)
        return {"cue_id": cue_id, "deleted": True}


def rebuild_all(force: bool = False) -> dict:
    """Synthesise every cue whose text has changed. The browser equivalent of
    running tools/build_show.py."""
    with _EDIT_LOCK:
        data = load_cues()
        built, skipped, failed = [], [], []
        for _section, cue in _iter_cues(data):
            try:
                r = _synthesise_cue(data, cue, force=force)
                (built if r.get("synthesised") else skipped).append(cue["id"])
            except Exception as e:
                log.exception("rebuild failed for %s", cue["id"])
                failed.append({"cue_id": cue["id"], "error": str(e)})
        _rewrite_boss_script(data)
        return {"built": built, "skipped": skipped, "failed": failed}


def motion_catalog() -> dict:
    """Valid motion names, so the editor can offer a list instead of letting
    someone type a name that will silently do nothing on stage."""
    import conversation as conv
    return {"emotions": list(conv.EMOTION_NAMES),
            "dances": list(conv.DANCE_NAMES),
            "directions": list(conv.HEAD_DIRECTIONS)}
