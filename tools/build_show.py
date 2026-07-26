"""Generate the show: cue audio + the boss's printed script.

Reads show/cues.json, synthesises every cue that has `text` using Gemini TTS
(same voice as the live conversation, so scripted and live Reachy sound
identical), and writes:

    show/audio/<cue_id>.wav   24 kHz mono PCM16 — the format the robot player
                              already consumes, no conversion at playback
    show/manifest.json        per-cue text hash + duration; the hash means an
                              unchanged line is never re-synthesised (and never
                              re-billed)
    docs/SHOW_SCRIPT.md       the operator/boss cue sheet, generated from the
                              same cues.json so paper and audio cannot drift

    .venv\\Scripts\\python.exe tools\\build_show.py
    .venv\\Scripts\\python.exe tools\\build_show.py --force        # re-synth everything
    .venv\\Scripts\\python.exe tools\\build_show.py --only joke    # one cue
    .venv\\Scripts\\python.exe tools\\build_show.py --script-only  # no API calls
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import conversation as conv

ROOT = Path(__file__).resolve().parent.parent
SHOW_DIR = ROOT / "show"
CUES_PATH = SHOW_DIR / "cues.json"
AUDIO_DIR = SHOW_DIR / "audio"
MANIFEST_PATH = SHOW_DIR / "manifest.json"
SCRIPT_PATH = ROOT / "docs" / "SHOW_SCRIPT.md"

TTS_RATE = 24000  # what the TTS models emit, and what stream_chunk() expects

# The free tier allows 10 TTS requests per minute per model. Space calls out
# rather than sprinting into a 429 — a full build is ~15 lines, so the pacing
# costs about a minute and a half and never fails halfway.
MIN_REQUEST_INTERVAL_S = 6.5
MAX_RETRIES = 3
DEFAULT_RETRY_S = 40.0
_last_request_t = 0.0


# ----- cue file -----

def load_cues() -> dict:
    data = json.loads(CUES_PATH.read_text(encoding="utf-8"))
    seen_ids: dict[str, str] = {}
    seen_keys: dict[str, str] = {}
    for section in data["sections"]:
        for cue in section["cues"]:
            cid = cue["id"]
            if cid in seen_ids:
                raise SystemExit(f"duplicate cue id {cid!r}")
            seen_ids[cid] = section["id"]
            key = cue.get("hotkey")
            if key:
                if key in seen_keys:
                    raise SystemExit(
                        f"hotkey {key!r} used by both {seen_keys[key]!r} and {cid!r}")
                seen_keys[key] = cid
            if not cue.get("text") and not cue.get("motion"):
                raise SystemExit(f"cue {cid!r} has neither text nor motion")
    return data


def iter_cues(data: dict):
    for section in data["sections"]:
        for cue in section["cues"]:
            yield section, cue


def text_hash(text: str, voice: str, style: str) -> str:
    """Changes when the line, the voice, or the delivery style changes."""
    return hashlib.sha256(f"{voice}\x00{style}\x00{text}".encode("utf-8")).hexdigest()[:16]


# ----- validation against the installed motion catalogs -----

def validate_motions(data: dict) -> list[str]:
    problems = []
    for _section, cue in iter_cues(data):
        motion = cue.get("motion")
        if not motion:
            continue
        kind, name = motion.get("type"), motion.get("name", "")
        if kind == "emotion" and conv.EMOTION_NAMES and name not in conv.EMOTION_NAMES:
            problems.append(f"{cue['id']}: unknown emotion {name!r}")
        elif kind == "dance" and conv.DANCE_NAMES and name not in conv.DANCE_NAMES:
            problems.append(f"{cue['id']}: unknown dance {name!r}")
        elif kind == "head" and motion.get("direction") not in conv.HEAD_DIRECTIONS:
            problems.append(f"{cue['id']}: unknown head direction {motion.get('direction')!r}")
        elif kind not in ("emotion", "dance", "head"):
            problems.append(f"{cue['id']}: unknown motion type {kind!r}")
    return problems


# ----- synthesis -----

def _retry_delay_from(err: Exception) -> float:
    """Honour the server's own retryDelay when it tells us one."""
    m = re.search(r"'retryDelay':\s*'(\d+(?:\.\d+)?)s'", str(err))
    if m:
        return float(m.group(1)) + 2.0
    m = re.search(r"retry in (\d+(?:\.\d+)?)s", str(err))
    return float(m.group(1)) + 2.0 if m else DEFAULT_RETRY_S


def synthesise(client, model: str, voice: str, style: str, text: str) -> np.ndarray:
    """One line -> int16 mono @ 24 kHz. The style prefix is an instruction to
    the TTS model, not something it reads aloud.

    Paced to stay inside the per-minute quota, and retried on 429 using the
    delay the API asks for."""
    global _last_request_t
    from google.genai import types

    prompt = f"{style}\n\n{text}" if style else text
    config = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
            )
        ),
    )

    resp = None
    for attempt in range(1, MAX_RETRIES + 1):
        wait = MIN_REQUEST_INTERVAL_S - (time.monotonic() - _last_request_t)
        if wait > 0:
            time.sleep(wait)
        try:
            _last_request_t = time.monotonic()
            resp = client.models.generate_content(model=model, contents=prompt,
                                                  config=config)
            break
        except Exception as e:
            if "RESOURCE_EXHAUSTED" not in str(e) and "429" not in str(e):
                raise
            if attempt == MAX_RETRIES:
                raise
            delay = _retry_delay_from(e)
            print(f"\n      rate limited; waiting {delay:.0f}s "
                  f"(attempt {attempt}/{MAX_RETRIES})… ", end="", flush=True)
            time.sleep(delay)

    part = resp.candidates[0].content.parts[0]
    raw = part.inline_data.data
    mime = part.inline_data.mime_type or ""
    if "l16" not in mime and "pcm" not in mime:
        raise RuntimeError(f"unexpected TTS mime type {mime!r}")
    if f"rate={TTS_RATE}" not in mime.replace(" ", ""):
        raise RuntimeError(f"unexpected TTS sample rate in {mime!r}; expected {TTS_RATE}")
    return np.frombuffer(raw, dtype=np.int16)


# ----- the boss's script -----

def write_script(data: dict, manifest: dict) -> None:
    lines = [
        "# Reachy Mini — show script",
        "",
        "Generated by `tools/build_show.py` from `show/cues.json`. Do not edit by",
        "hand: edit the cue file and re-run the generator, or the paper and the",
        "audio will disagree.",
        "",
        "The operator fires cues from `/show` on the laptop. **Space** fires the next",
        "cue in order and advances; the letter/number keys fire any cue directly;",
        "**Esc** stops everything and returns Reachy to breathing.",
        "",
    ]
    for section in data["sections"]:
        lines += [f"## {section['title']}", ""]
        lines += ["| Key | Cue | הבוס אומר | ריצי אומר | תנועה | אורך |",
                  "|:---:|---|---|---|---|---:|"]
        for cue in section["cues"]:
            entry = manifest.get("cues", {}).get(cue["id"], {})
            dur = entry.get("duration_s")
            motion = cue.get("motion") or {}
            motion_txt = motion.get("name") or motion.get("direction") or "—"
            if motion.get("type"):
                motion_txt = f"{motion['type']}: {motion_txt}"
            lines.append(
                f"| `{cue.get('hotkey', '')}` | {cue.get('label', cue['id'])} "
                f"| {cue.get('boss_cue', '')} | {cue.get('text', '—')} "
                f"| {motion_txt} | {f'{dur:.1f}s' if dur else '—'} |")
        lines.append("")
    lines += [
        "## On the day",
        "",
        "1. Robot and laptop on the same WiFi; `tools\\preflight.py` passes.",
        "2. Open the dashboard, wait for **Idle**, then open `/show` in a second tab.",
        "3. Keep the `/show` tab on the laptop screen only — never on the projector.",
        "4. The SAVE cues (`s`, `d`, `f`) exist for when a live conversation stalls.",
        "   They interrupt whatever Reachy is saying.",
        "",
    ]
    SCRIPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCRIPT_PATH.write_text("\n".join(lines), encoding="utf-8")


# ----- main -----

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Generate show audio + script.")
    ap.add_argument("--force", action="store_true", help="Re-synthesise every line.")
    ap.add_argument("--only", default=None, help="Only this cue id.")
    ap.add_argument("--script-only", action="store_true",
                    help="Regenerate docs/SHOW_SCRIPT.md without calling the API.")
    args = ap.parse_args()

    data = load_cues()
    voice = data.get("voice", conv.GEMINI_VOICE)
    style = data.get("style", "")
    model = data.get("tts_model", "gemini-3.1-flash-tts-preview")

    problems = validate_motions(data)
    if problems:
        print("Motion names that do not exist in the installed catalogs:")
        for p in problems:
            print(f"  {p}")
        if conv.EMOTION_NAMES:
            return 1
        print("  (catalogs unavailable here — skipping the check)")

    manifest = {"voice": voice, "model": model, "cues": {}}
    if MANIFEST_PATH.exists():
        try:
            manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            manifest.setdefault("cues", {})
        except Exception:
            print("manifest unreadable; rebuilding it")

    if args.script_only:
        write_script(data, manifest)
        print(f"wrote {SCRIPT_PATH.relative_to(ROOT)}")
        return 0

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    client = None
    generated = skipped = motion_only = 0

    for _section, cue in iter_cues(data):
        cid = cue["id"]
        if args.only and cid != args.only:
            continue
        text = cue.get("text")
        if not text:
            manifest["cues"].pop(cid, None)
            motion_only += 1
            continue

        wav_path = AUDIO_DIR / f"{cid}.wav"
        h = text_hash(text, voice, style)
        entry = manifest["cues"].get(cid)
        if (not args.force and entry and entry.get("hash") == h and wav_path.exists()):
            skipped += 1
            continue

        if client is None:
            from google import genai
            client = genai.Client(api_key=conv.get_api_key())

        print(f"  synthesising {cid}… ", end="", flush=True)
        samples = synthesise(client, model, voice, style, text)
        sf.write(wav_path, samples, TTS_RATE, subtype="PCM_16")
        duration = samples.size / TTS_RATE
        manifest["cues"][cid] = {"hash": h, "duration_s": round(duration, 2),
                                 "file": f"audio/{cid}.wav", "text": text}
        generated += 1
        print(f"{duration:.1f}s")
        # Save after every line: a quota error partway through then costs only
        # the lines it did not reach, not the whole run.
        MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                 encoding="utf-8")

    manifest["voice"] = voice
    manifest["model"] = model
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    write_script(data, manifest)

    total_audio = sum(e.get("duration_s", 0) for e in manifest["cues"].values())
    print(f"\n{generated} generated, {skipped} unchanged, {motion_only} motion-only")
    print(f"total spoken audio: {total_audio:.0f}s across {len(manifest['cues'])} lines")
    print(f"wrote {MANIFEST_PATH.relative_to(ROOT)} and {SCRIPT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
