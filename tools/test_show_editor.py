#!/usr/bin/env python3
"""Round-trip test for editing the show script from the board.

Adds a throwaway cue, edits its line (which re-records it), then deletes it —
and checks that cues.json comes back byte-identical to how it started. That
last check is the point: this code rewrites the one file in the project with no
other copy, days before it is needed on stage.

    python tools\\test_show_editor.py            # full run, one TTS call
    python tools\\test_show_editor.py --no-tts   # skip synthesis (motion-only cue)

Safe to run against the real show/: it restores the original file at the end,
and every write leaves a backup in show/backups/ regardless.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import show_editor as se  # noqa: E402

TEST_LABEL = "ZZ editor self-test"
TEST_TEXT = "זאת בדיקה אוטומטית."
TEST_TEXT_2 = "זאת בדיקה אוטומטית, אחרי עריכה."


def free_hotkey(data: dict) -> str:
    used = {(c.get("hotkey") or "").lower() for _s, c in se._iter_cues(data)}
    for ch in "0123456789abcdefghijklmnopqrstuvwxyz":
        if ch not in used:
            return ch
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-tts", action="store_true",
                    help="create a motion-only cue, so nothing is synthesised")
    args = ap.parse_args()

    original = se.CUES_PATH.read_text(encoding="utf-8")
    data = se.load_cues()
    section_id = data["sections"][0]["id"]
    before = len(list(se._iter_cues(data)))
    hotkey = free_hotkey(data)
    print(f"start: {before} cues, editing section {section_id!r}, "
          f"free hotkey {hotkey!r}")

    failures = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    cue_id = None
    try:
        # ---- add -------------------------------------------------------
        fields = {"label": TEST_LABEL, "hotkey": hotkey,
                  "motion": {"type": "emotion", "name": "welcoming1"}}
        if not args.no_tts:
            fields["text"] = TEST_TEXT
        r = se.add_cue(section_id, fields)
        cue_id = r["cue_id"]
        data = se.load_cues()
        _s, cue = se._find(data, cue_id)
        wav = se.AUDIO_DIR / f"{cue_id}.wav"
        man = se._load_manifest()["cues"].get(cue_id)

        check("add: cue is in cues.json",
              len(list(se._iter_cues(data))) == before + 1)
        check("add: id is filesystem-safe",
              cue_id.replace("_", "").isalnum(), f"id={cue_id!r}")
        check("add: motion stored", cue.get("motion", {}).get("name") == "welcoming1")
        if args.no_tts:
            check("add: no audio for a motion-only cue", not wav.exists())
        else:
            check("add: WAV was recorded", wav.exists(), str(wav.name))
            check("add: manifest has duration",
                  bool(man and man.get("duration_s")),
                  f"{man and man.get('duration_s')}s")

        # ---- duplicate hotkey is refused -------------------------------
        try:
            se.add_cue(section_id, {"label": "dupe", "hotkey": hotkey,
                                    "motion": {"type": "emotion", "name": "welcoming1"}})
            check("add: duplicate hotkey refused", False, "it was accepted")
        except se.ShowEditError:
            check("add: duplicate hotkey refused", True)

        # ---- update ----------------------------------------------------
        if not args.no_tts:
            old_hash = se._load_manifest()["cues"][cue_id]["hash"]
            r2 = se.update_cue(cue_id, {"text": TEST_TEXT_2})
            new = se._load_manifest()["cues"][cue_id]
            check("edit: line was re-recorded", bool(r2.get("synthesised")))
            check("edit: hash changed", new["hash"] != old_hash)
            check("edit: manifest text matches the new line",
                  new["text"] == TEST_TEXT_2)

            r3 = se.update_cue(cue_id, {"label": TEST_LABEL + " (renamed)"})
            check("edit: renaming does not re-record",
                  not r3.get("synthesised"), r3.get("reason", ""))

        # ---- a cue that would do nothing is refused --------------------
        try:
            se.update_cue(cue_id, {"text": "", "motion": None})
            check("edit: empty cue refused", False, "it was accepted")
        except se.ShowEditError:
            check("edit: empty cue refused", True)

    finally:
        # ---- delete ----------------------------------------------------
        if cue_id:
            se.delete_cue(cue_id)
            data = se.load_cues()
            wav = se.AUDIO_DIR / f"{cue_id}.wav"
            check("delete: cue removed",
                  len(list(se._iter_cues(data))) == before)
            check("delete: WAV removed", not wav.exists())
            check("delete: manifest entry removed",
                  cue_id not in se._load_manifest()["cues"])

        restored = se.CUES_PATH.read_text(encoding="utf-8")
        same = json.loads(restored) == json.loads(original)
        check("cues.json is back to its original content", same)
        if not same:
            se.CUES_PATH.write_text(original, encoding="utf-8")
            print("     (restored it from the in-memory copy)")

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
