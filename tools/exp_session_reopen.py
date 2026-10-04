#!/usr/bin/env python3
"""Does a Gemini Live session still work after a hard-abort reopen?

In `conversations/2026-07-27_17-32-37`, turns 5 and 6 sent near-silence (1
speech frame each) and got no reply — which is what a model should do with
silence. Each no-reply tripped the 25 s hard-abort, which closed and reopened
the session. Turn 7 then sent *real* speech (max_prob 0.999, 36 % speech
frames) to the freshly reopened session and also got nothing.

Two competing explanations, and reasoning cannot separate them:
  (a) the reopened session is fine, and turn 7 was refused for its own reason
      (rate limiting after three sessions in 90 s, or more room noise);
  (b) a session reopened after a hard-abort is not actually usable.

(b) would be the more serious bug, so it gets measured rather than assumed.
This replays the exact sequence against the live API:

    control  -> speech on a fresh session          (expect: replies)
    silence  -> 1 s of near-silence, same session  (expect: no reply)
    reopen   -> close, reconnect with the same cfg
    retry    -> the same speech on the new session (the question)

    python tools\\exp_session_reopen.py

Needs the API key and network; no robot and no microphone.
"""

from __future__ import annotations

import asyncio
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:                       # Hebrew transcripts vs a cp1252 console
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from google import genai                                    # noqa: E402
from google.genai import types                              # noqa: E402
import conversation as conv                                 # noqa: E402

BENCH = ROOT / "archive" / "bench_input_he.wav"
# Long enough for the model to have started replying on a good turn; the real
# hard-abort waits 25 s, which would make this experiment needlessly slow.
LISTEN_S = 20.0


def load_pcm16(path: Path) -> bytes:
    with wave.open(str(path)) as w:
        raw = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        rate, ch = w.getframerate(), w.getnchannels()
    if ch > 1:
        raw = raw.reshape(-1, ch)[:, 0]
    if rate != conv.GEMINI_INPUT_RATE:
        from scipy.signal import resample_poly
        from math import gcd
        g = gcd(rate, conv.GEMINI_INPUT_RATE)
        f = raw.astype(np.float32) / 32768.0
        f = resample_poly(f, conv.GEMINI_INPUT_RATE // g, rate // g)
        raw = np.clip(f * 32768.0, -32768, 32767).astype(np.int16)
    return raw.tobytes()


def near_silence(seconds: float = 1.0) -> bytes:
    """What turn 5 actually sent: room tone, no speech."""
    n = int(seconds * conv.GEMINI_INPUT_RATE)
    rng = np.random.default_rng(0)
    return (rng.normal(0, 12, n)).astype(np.int16).tobytes()


async def one_turn(session, pcm: bytes, label: str) -> dict:
    """Send one turn, drain until end-of-turn or LISTEN_S, report what came."""
    t0 = time.perf_counter()
    await session.send_realtime_input(
        audio=types.Blob(data=pcm,
                         mime_type=f"audio/pcm;rate={conv.GEMINI_INPUT_RATE}"))
    await session.send_realtime_input(audio_stream_end=True)

    got = {"label": label, "audio_chunks": 0, "audio_bytes": 0, "text": "",
           "tool_calls": 0, "first_chunk_s": None, "end_of_turn": False}
    try:
        async def drain():
            async for resp in session.receive():
                sc = getattr(resp, "server_content", None)
                tc = getattr(resp, "tool_call", None)
                if tc is not None:
                    got["tool_calls"] += 1
                    # The declarations are BLOCKING, so the FunctionResponse is
                    # also the signal that lets the model continue. Without it
                    # the turn stalls forever and every row below reads SILENT
                    # for the wrong reason. Scheduling follows the same rule as
                    # the app: SILENT only once audio has started.
                    await session.send_tool_response(function_responses=[
                        types.FunctionResponse(
                            id=getattr(fc, "id", None),
                            name=getattr(fc, "name", ""),
                            response={"status": "queued"},
                            scheduling=conv._response_scheduling(
                                got["audio_chunks"] > 0),
                        )
                        for fc in (tc.function_calls or [])
                    ])
                if sc is not None:
                    mt = getattr(sc, "model_turn", None)
                    if mt is not None:
                        for part in (mt.parts or []):
                            inline = getattr(part, "inline_data", None)
                            if inline is not None and inline.data:
                                got["audio_chunks"] += 1
                                got["audio_bytes"] += len(inline.data)
                                if got["first_chunk_s"] is None:
                                    got["first_chunk_s"] = time.perf_counter() - t0
                            if getattr(part, "text", None):
                                got["text"] += part.text
                    for attr in ("input_transcription", "output_transcription"):
                        tr = getattr(sc, attr, None)
                        if tr is not None and getattr(tr, "text", None):
                            got["text"] += f"[{attr}: {tr.text}]"
                    if getattr(sc, "turn_complete", False):
                        got["end_of_turn"] = True
                        return
        await asyncio.wait_for(drain(), timeout=LISTEN_S)
    except asyncio.TimeoutError:
        pass
    got["elapsed_s"] = time.perf_counter() - t0
    return got


def show(r: dict) -> None:
    verdict = "REPLIED" if (r["audio_chunks"] or r["text"]) else "SILENT"
    print(f"  {r['label']:<28} {verdict:<8} "
          f"audio_chunks={r['audio_chunks']:<4} bytes={r['audio_bytes']:<7} "
          f"tools={r['tool_calls']} eot={r['end_of_turn']} "
          f"first={r['first_chunk_s'] if r['first_chunk_s'] is None else round(r['first_chunk_s'],2)} "
          f"({r['elapsed_s']:.1f}s)")
    if r["text"]:
        print(f"      text: {r['text'][:160]}")


async def main() -> int:
    if not BENCH.is_file():
        raise SystemExit(f"missing {BENCH}")
    speech = load_pcm16(BENCH)
    silence = near_silence(1.0)
    print(f"speech: {len(speech)//2} samples  silence: {len(silence)//2} samples\n")

    client = genai.Client(api_key=conv.get_api_key())
    cfg = conv.build_live_config()
    results = []

    print("session #1 (fresh):")
    async with client.aio.live.connect(model=conv.GEMINI_MODEL,
                                       config=cfg) as s:
        results.append(await one_turn(s, speech, "control: speech"))
        show(results[-1])
        results.append(await one_turn(s, silence, "silence (like turn 5)"))
        show(results[-1])
        # The load-bearing question for the fix: is the session still usable
        # after a silent turn, or does silence actually poison it? If this
        # replies, then tearing the session down on silence is unnecessary --
        # and the 25 s hard-abort plus reconnect is pure demo-wrecking delay.
        results.append(await one_turn(s, speech, "speech AFTER silence (same session)"))
        show(results[-1])

    print("\n-- closed and reopening, as the hard-abort path does --\n")
    await asyncio.sleep(0.5)

    print("session #2 (reopened):")
    async with client.aio.live.connect(model=conv.GEMINI_MODEL,
                                       config=cfg) as s:
        results.append(await one_turn(s, speech, "retry: same speech"))
        show(results[-1])
        results.append(await one_turn(s, speech, "retry 2: speech again"))
        show(results[-1])

    control, sil, after_sil, retry, retry2 = results
    print("\n" + "=" * 66)
    ok = lambda r: bool(r["audio_chunks"] or r["text"])          # noqa: E731
    if ok(control) and not ok(sil) and ok(after_sil):
        print("A silent turn does NOT poison the session: the very next turn on\n"
              "the SAME session answered normally. So the hard-abort's\n"
              "close-and-reopen is not needed for this failure, and the 25 s it\n"
              "waits first is dead air in front of an audience.\n"
              "=> (1) stop sending speechless audio at all;\n"
              "   (2) when Gemini does stay silent, go back to listening\n"
              "       instead of tearing the session down.")
    elif ok(control) and not ok(after_sil) and ok(retry):
        print("A silent turn DOES poison the session -- the next turn on the same\n"
              "session stayed silent, while a reopened session recovered.\n"
              "=> the reopen must stay; keep it but make it much faster.")
    elif ok(control) and not ok(retry):
        print("Reopen is BROKEN: a fresh session answered, the reopened one did\n"
              "not, on identical audio. The hard-abort recovery path needs work.")
    elif not ok(control):
        print("Even a fresh session did not answer the control clip. Something\n"
              "broader is wrong (quota / model / key) — rerun before concluding.")
    else:
        print("Mixed result; read the rows above.")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
