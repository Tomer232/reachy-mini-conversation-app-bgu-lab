#!/usr/bin/env python3
"""Pre-flight for the backend picker: hold a real conversation on every
brain / language / voice combination, with no robot, and report.

    .venv\\Scripts\\python.exe tools\\check_backends.py            # all combos with a key
    .venv\\Scripts\\python.exe tools\\check_backends.py gemini-3.8:he gpt-live-1:en+el
    .venv\\Scripts\\python.exe tools\\check_backends.py --play     # hear the replies

Each combination runs tools/dry_run.py (the real dashboard, the laptop as the
robot, a WAV file as the microphone), picks the combination through the same
REST call the dashboard's dropdown makes, presses Start, and plays three
turns from tools/test_audio/: a question, a follow-up, and a goodbye -- which
should end the conversation by itself through the end-phrase check.

A combination passes when every turn got a reply with audio, the robot's
words came back as text, and the goodbye ended the conversation. The audio
is in conversations/<timestamp>/turn_NNN.wav to listen to afterwards.

Combination syntax: <brain>:<language>[+el]   e.g. gemini-3.1:he, gpt-live-1:en+el
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import httpx

AUDIO = ROOT / "tools" / "test_audio"
PY = sys.executable


def all_combos() -> list:
    import providers
    out = []
    for b in providers.BRAINS:
        for lang in ("he", "en", "auto"):
            out.append("{}:{}".format(b["id"], lang))
    for b in providers.BRAINS:
        out.append("{}:he+el".format(b["id"]))
    return out


def _has_keys(combo: dict) -> bool:
    """Skip, rather than fail, a combination nobody has a key for yet."""
    import credentials
    import providers
    need = [providers.brain(combo["brain"])["provider"]]
    if combo["elevenlabs"]:
        need.append(credentials.ELEVENLABS)
    for p in need:
        try:
            credentials.resolve(p, use_hub=False)
        except Exception:
            print("skip {} (no {} key)".format(combo["name"], p))
            return False
    return True


def parse_combo(text: str) -> dict:
    el = text.endswith("+el")
    base = text[:-3] if el else text
    brain, _, lang = base.partition(":")
    return {"brain": brain, "language": lang or "he", "elevenlabs": el,
            "name": text}


async def run_combo(combo: dict, port: int, play: bool, timeout_s: float) -> dict:
    import websockets
    lang = combo["language"]
    # Auto switches language every turn: Hebrew, English, Hebrew goodbye.
    langs = ["he", "en", "he"] if lang == "auto" else [lang] * 3
    wavs = [str(AUDIO / "{}_{}.wav".format(l, i)) for l, i in zip(langs, (1, 2, 3))]
    result_langs = langs
    cmd = [PY, "-u", str(ROOT / "tools" / "dry_run.py"), "--no-browser",
           "--port", str(port)]
    if not play:
        cmd.append("--mute")
    for w in wavs:
        cmd += ["--mic-wav", w]
    log_path = ROOT / "conversations" / "check_backends_{}.log".format(
        combo["name"].replace(":", "_").replace("+", "_"))
    log_path.parent.mkdir(exist_ok=True)
    log_fh = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=log_fh,
                            stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:{}".format(port)
    result = {"combo": combo["name"], "ok": False, "turns": [], "ended": None,
              "errors": [], "log": str(log_path)}
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            deadline = time.time() + 120
            state = None
            while time.time() < deadline:
                try:
                    state = (await http.get(base + "/api/status")).json()["state"]
                    if state in ("IDLE_BREATHING", "STOPPED"):
                        break
                except Exception:
                    pass
                await asyncio.sleep(1.0)
            if state != "IDLE_BREATHING":
                result["errors"].append("dashboard never became idle (state {})".format(state))
                return result
            r = await http.put(base + "/api/backend", json={
                "brain": combo["brain"], "language": lang,
                "elevenlabs": combo["elevenlabs"]})
            if r.status_code != 200:
                result["errors"].append("picker refused: " + r.text)
                return result
            async with websockets.connect("ws://127.0.0.1:{}/ws".format(port),
                                          max_size=None) as ws:
                r = await http.post(base + "/api/conversation/start")
                if r.status_code != 200:
                    result["errors"].append("start refused: " + r.text)
                    return result
                conv_dir = Path(r.json()["dir"])
                result["dir"] = str(conv_dir)
                t_end = time.time() + timeout_s
                turns: dict = {}
                while time.time() < t_end:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), t_end - time.time())
                    except asyncio.TimeoutError:
                        break
                    m = json.loads(raw)
                    ev = m.get("event")
                    if ev == "conversation.started":
                        result["backend"] = m.get("backend")
                    elif ev in ("transcript.user", "transcript.robot"):
                        t = turns.setdefault(m["turn_id"], {"turn": m["turn_id"]})
                        t["user" if ev == "transcript.user" else "robot"] = m["text"]
                    elif ev == "turn.aborted":
                        t = turns.setdefault(m["turn_id"], {"turn": m["turn_id"]})
                        t["aborted"] = m.get("reason")
                    elif ev == "error":
                        result["errors"].append("{}: {}".format(m.get("where"), m.get("message")))
                    elif ev == "log" and m.get("level") in ("ERROR", "CRITICAL"):
                        result["errors"].append(m.get("msg", "")[:300])
                    elif ev == "conversation.ended":
                        result["ended"] = m.get("reason")
                        break
                if result["ended"] is None:
                    await http.post(base + "/api/conversation/end")
                    result["ended"] = "timeout ({}s)".format(int(timeout_s))
                    await asyncio.sleep(3)
                result["turns"] = [turns[k] for k in sorted(turns)]
                _add_timings(result, conv_dir)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()
        log_fh.close()

    replied = [t for t in result["turns"] if t.get("robot") and not t.get("aborted")]
    with_audio = [t for t in result["turns"] if t.get("audio_s", 0) > 0.3]
    result["ok"] = (len(replied) >= 3 and len(with_audio) >= 3
                    and result["ended"] == "end_phrase")
    # Every reply must be in the language that turn was spoken in.
    for t, want in zip(result["turns"], result_langs):
        got = "he" if _is_hebrew(t.get("robot", "")) else "en"
        if t.get("robot") and got != want:
            result["ok"] = False
            result["errors"].append("turn {}: spoken in {}, answered in {}".format(
                t["turn"], want, got))
    return result


def _is_hebrew(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum("\u0590" <= c <= "\u05ff" for c in letters) > len(letters) / 2


def _add_timings(result: dict, conv_dir: Path) -> None:
    events = conv_dir / "events.jsonl"
    if not events.exists():
        return
    by_turn: dict = {}
    for line in events.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except Exception:
            continue
        tid = e.get("turn_id")
        if tid is None:
            continue
        d = by_turn.setdefault(tid, {})
        if e["event"] == "gemini.recv.first_chunk":
            d["first_audio_s"] = e.get("latency_ms_from_user_speech_end", 0) / 1000
        elif e["event"] == "turn.end":
            d["audio_s"] = e.get("samples_sent_to_robot", 0) / 16000
        elif e["event"] == "tool.dispatched":
            d.setdefault("tools", []).append(e.get("function_name"))
    for t in result["turns"]:
        t.update(by_turn.get(t["turn"], {}))


def report(r: dict) -> None:
    mark = "PASS" if r["ok"] else "FAIL"
    print("\n[{}] {}   ({})".format(mark, r["combo"], r.get("backend", "")))
    for t in r["turns"]:
        if t.get("aborted"):
            print("   turn {}: aborted ({})".format(t["turn"], t["aborted"]))
            continue
        print("   turn {}: first audio {:.1f}s after speech end, {:.1f}s of speech{}".format(
            t["turn"], t.get("first_audio_s", 0), t.get("audio_s", 0),
            ", motion: " + ",".join(t["tools"]) if t.get("tools") else ""))
        print("      heard: {}".format(t.get("user", "")))
        print("      said:  {}".format(t.get("robot", "")))
    print("   ended: {}".format(r["ended"]))
    for e in r["errors"][:6]:
        print("   ! {}".format(e))
    if not r["ok"]:
        print("   log: {}".format(r["log"]))


async def amain() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("combos", nargs="*")
    ap.add_argument("--play", action="store_true", help="play replies on the speakers")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--timeout", type=float, default=150.0)
    args = ap.parse_args()
    combos = [parse_combo(c) for c in (args.combos or all_combos())]
    if not args.combos:
        combos = [c for c in combos if _has_keys(c)]
    # Each combination is selected through the dashboard, which saves it to
    # backend.json. Put back whatever the person had chosen.
    backend_json = ROOT / "backend.json"
    saved = backend_json.read_text(encoding="utf-8") if backend_json.exists() else None
    results = []
    try:
        for c in combos:
            print("... {}".format(c["name"]), flush=True)
            r = await run_combo(c, args.port, args.play, args.timeout)
            report(r)
            results.append(r)
    finally:
        if saved is None:
            backend_json.unlink(missing_ok=True)
        else:
            backend_json.write_text(saved, encoding="utf-8")
    print("\n" + "=" * 60)
    for r in results:
        print("{}  {}".format("PASS" if r["ok"] else "FAIL", r["combo"]))
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
