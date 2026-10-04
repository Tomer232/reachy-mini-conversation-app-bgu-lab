#!/usr/bin/env python3
"""The GPT-Live and ElevenLabs adapters against local stand-ins, no keys needed.

    .venv\\Scripts\\python.exe tools\\test_fake_backends.py

Two small WebSocket servers speak the documented protocols (see the
docstrings of providers/gpt_live.py and providers/elevenlabs_voice.py):

  * a fake gpt-live-1 that listens to the uplink like the real one must --
    speech energy, then silence -- and answers with a tone and a transcript,
    paced in real time, with no end-of-reply event;
  * a fake ElevenLabs Text-to-Dialogue socket that turns each text chunk into
    audio and ends with is_final.

Then whole conversations run through tools/dry_run.py exactly as
tools/check_backends.py runs them against the real services. This proves the
adapters' plumbing -- live mic streaming, the silence pump, end-of-reply
detection, reconnect with history, ElevenLabs ordering and fallback -- and
nothing about the real vendors, which is check_backends.py's job.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import numpy as np

GPT_PORT = 8871
EL_PORT = 8872
DEAD_PORT = 8873          # nothing listens here: the ElevenLabs-down case

USER_TEXTS = ["Hi Reachy, tell me a joke.", "What do you like doing?",
              "Thanks, goodbye!"]
STATS = {"gpt_sessions": 0, "gpt_restored_inputs": [], "gpt_audio_s": 0.0,
         "el_texts": 0, "expire_after_turn": None}


def tone(seconds: float, rate: int = 24000, freq: float = 330.0) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    return (0.2 * np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16).tobytes()


async def fake_gpt_live(ws) -> None:
    STATS["gpt_sessions"] += 1
    start = json.loads(await ws.recv())
    assert start["type"] == "session.start", start
    sess = start["session"]
    assert sess["audio"]["format"]["rate"] == 24000
    if sess.get("input"):
        STATS["gpt_restored_inputs"].append(len(sess["input"]))
    await ws.send(json.dumps({"type": "session.started",
                              "session": {"id": "sess_fake_%d" % STATS["gpt_sessions"]}}))
    speaking = False
    quiet_s = 0.0
    turn = getattr(fake_gpt_live, "turns_done", 0)
    replying: "asyncio.Task | None" = None

    async def reply(n: int) -> None:
        await ws.send(json.dumps({"type": "session.input_transcript.delta",
                                  "delta": USER_TEXTS[min(n, len(USER_TEXTS) - 1)],
                                  "start_ms": 0, "end_ms": 100}))
        words = ["Sure!", " Here", " is", " my", " answer", " number %d." % (n + 1)]
        audio = tone(1.2)
        step = len(audio) // len(words) & ~1
        for i, w in enumerate(words):
            await ws.send(json.dumps({"type": "session.output_audio.delta",
                                      "delta": base64.b64encode(
                                          audio[i * step:(i + 1) * step]).decode()}))
            await ws.send(json.dumps({"type": "session.output_transcript.delta",
                                      "delta": w, "start_ms": 0, "end_ms": 1}))
            await asyncio.sleep(0.2)
        fake_gpt_live.turns_done = n + 1
        if STATS["expire_after_turn"] is not None and n + 1 == STATS["expire_after_turn"]:
            STATS["expire_after_turn"] = None
            await asyncio.sleep(0.3)
            await ws.send(json.dumps({"type": "session.closed", "reason": "expired"}))
            await ws.close()

    try:
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") == "session.close":
                await ws.send(json.dumps({"type": "session.closed",
                                          "reason": "close_requested"}))
                return
            if msg.get("type") != "session.input_audio.append":
                continue
            pcm = np.frombuffer(base64.b64decode(msg["audio"]), dtype=np.int16)
            STATS["gpt_audio_s"] += pcm.size / 24000
            loud = pcm.size and np.abs(pcm).max() > 500
            if loud:
                speaking, quiet_s = True, 0.0
            elif speaking:
                quiet_s += pcm.size / 24000
                if quiet_s >= 0.6 and (replying is None or replying.done()):
                    speaking = False
                    replying = asyncio.create_task(reply(turn))
                    turn += 1
    except Exception:
        pass


async def fake_elevenlabs(ws) -> None:
    first = json.loads(await ws.recv())
    assert first.get("voices"), first
    try:
        async for raw in ws:
            msg = json.loads(raw)
            for item in msg.get("inputs") or []:
                STATS["el_texts"] += 1
                audio = tone(0.05 * max(1, len(item["text"])), freq=550.0)
                await ws.send(json.dumps({"audio": base64.b64encode(audio).decode()}))
            if msg.get("close_socket"):
                await ws.send(json.dumps({"audio": "", "is_final": True}))
                await ws.close()
                return
    except Exception:
        pass


def start_servers() -> None:
    from websockets.asyncio.server import serve

    async def main():
        async with serve(fake_gpt_live, "127.0.0.1", GPT_PORT), \
                serve(fake_elevenlabs, "127.0.0.1", EL_PORT):
            await asyncio.Future()

    threading.Thread(target=lambda: asyncio.run(main()), daemon=True).start()


async def amain() -> int:
    import check_backends as cb
    start_servers()
    await asyncio.sleep(0.5)
    env_base = {
        "OPENAI_API_KEY": "sk-fake-for-local-test",
        "ELEVENLABS_API_KEY": "el-fake-for-local-test",
        "REACHY_GPT_LIVE_URL": "ws://127.0.0.1:%d/v1/live/sessions" % GPT_PORT,
        "REACHY_ELEVENLABS_URL": "ws://127.0.0.1:%d/v1/text-to-dialogue/stream-input" % EL_PORT,
    }
    # The voice list would need the real API; give the picker a voice id.
    backend_json = ROOT / "backend.json"
    saved = backend_json.read_text(encoding="utf-8") if backend_json.exists() else None

    cases = [
        ("gpt-live-1 alone", "gpt-live-1:en", {}, None),
        ("gpt-live-1 + ElevenLabs", "gpt-live-1:he+el", {}, None),
        ("gpt-live-1, session expires after turn 1", "gpt-live-1:en", {}, 1),
        ("real Gemini 3.8 + fake ElevenLabs", "gemini-3.8:he+el", {}, None),
        ("Gemini 3.8 + ElevenLabs down -> own voice", "gemini-3.8:en+el",
         {"REACHY_ELEVENLABS_URL": "ws://127.0.0.1:%d/x" % DEAD_PORT}, None),
    ]
    failures = 0
    try:
        for title, combo, extra_env, expire in cases:
            os.environ.update(env_base)
            os.environ.update(extra_env)
            fake_gpt_live.turns_done = 0
            STATS["expire_after_turn"] = expire
            STATS["el_texts"] = 0
            sessions_before = STATS["gpt_sessions"]
            backend_json.write_text(json.dumps({"el_voice_id": "fake-voice",
                                                "el_voice_name": "Fake"}),
                                    encoding="utf-8")
            print("\n### " + title, flush=True)
            r = await cb.run_combo(cb.parse_combo(combo), 8798, False, 120.0)
            cb.report(r)
            ok = r["ok"]
            if "+el" in combo and "down" not in title and STATS["el_texts"] == 0:
                print("   ! ElevenLabs never received text")
                ok = False
            if expire is not None:
                opened = STATS["gpt_sessions"] - sessions_before
                restored = STATS["gpt_restored_inputs"][-1:] or [0]
                print("   sessions opened: %d, history restored: %s messages"
                      % (opened, restored[0]))
                ok = ok and opened >= 2 and restored[0] >= 2
            print("   => %s" % ("OK" if ok else "FAILED"))
            failures += 0 if ok else 1
            for k in extra_env:
                os.environ.pop(k, None)
    finally:
        if saved is None:
            backend_json.unlink(missing_ok=True)
        else:
            backend_json.write_text(saved, encoding="utf-8")
    print("\n%d case(s) failed" % failures if failures else "\nall cases passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
