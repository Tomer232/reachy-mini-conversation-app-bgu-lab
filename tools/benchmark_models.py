"""Benchmark Gemini Live models on a fixed Hebrew test phrase.

Measures, per model:
  - first_chunk_latency: time from audio_stream_end -> first audio byte received
  - total_latency: time to turn_complete
  - assistant_text: what Gemini said back (for subjective Hebrew quality)
  - input_transcription: what Gemini heard

We synthesize a short Hebrew utterance with Google TTS-equivalent... actually we
can't — the laptop mic test requires a human. So we instead feed a small
prerecorded WAV. If no WAV exists, we fall back to a saved Hebrew sample
recorded once.

For now the script accepts a WAV path as argv[1]. If omitted we use
archive/bench_input_he.wav (the saved Hebrew TTS sample). The script
resamples whatever it gets to 16 kHz mono int16 before sending.
"""
import sys
import time
import json
import base64
import asyncio
from pathlib import Path

# Force UTF-8 stdout so Hebrew prints on Windows
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

from google import genai
from google.genai import types

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "archive"
OUT_DIR.mkdir(exist_ok=True)


def get_api_key() -> str:
    import os
    k = os.environ.get("GEMINI_API_KEY")
    if k:
        return k.strip()
    p = ROOT / ".gemini_key"
    if p.exists():
        return p.read_text().strip()
    tomer = ROOT.parent / "reachy-mini llm gemini token.txt"
    return tomer.read_text().strip()


def load_audio_as_16k_int16(wav_path: Path) -> np.ndarray:
    audio, sr = sf.read(str(wav_path), dtype="int16", always_2d=False)
    if audio.ndim > 1:
        audio = audio[:, 0]
    if sr != 16000:
        # int16 -> float -> resample -> int16
        f = audio.astype(np.float32) / 32768.0
        from math import gcd
        g = gcd(sr, 16000)
        up = 16000 // g
        down = sr // g
        f2 = resample_poly(f, up, down)
        audio = np.clip(f2, -1.0, 1.0)
        audio = (audio * 32767).astype(np.int16)
    return audio


SYSTEM_PROMPT_HE = (
    "אתה Reachy Mini, רובוט שולחני קטן וידידותי. ענה תמיד בעברית. "
    "תגובות קצרות, משפט או שניים. היה חם וסקרן."
)


async def benchmark_one(client: "genai.Client", model: str, audio_int16: np.ndarray,
                        voice: str = "Aoede", use_lang_code: bool = True) -> dict:
    speech_cfg_kwargs = {
        "voice_config": types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
        ),
    }
    if use_lang_code:
        speech_cfg_kwargs["language_code"] = "he-IL"
    cfg = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        system_instruction=SYSTEM_PROMPT_HE,
        speech_config=types.SpeechConfig(**speech_cfg_kwargs),
    )

    result = {
        "model": model,
        "voice": voice,
        "lang_code": "he-IL" if use_lang_code else None,
        "error": None,
        "first_chunk_s": None,
        "total_s": None,
        "user_txt": "",
        "asst_txt": "",
        "audio_bytes": 0,
    }

    try:
        async with client.aio.live.connect(model=model, config=cfg) as session:
            t_send = time.perf_counter()
            await session.send_realtime_input(
                audio=types.Blob(data=audio_int16.tobytes(),
                                 mime_type="audio/pcm;rate=16000")
            )
            await session.send_realtime_input(audio_stream_end=True)

            first_chunk_t = None
            asst_parts = []
            user_parts = []
            total_audio = 0

            async for resp in session.receive():
                sc = resp.server_content
                if sc is None:
                    continue
                if sc.model_turn and sc.model_turn.parts:
                    for part in sc.model_turn.parts:
                        if part.inline_data and part.inline_data.data:
                            b = part.inline_data.data
                            if isinstance(b, str):
                                b = base64.b64decode(b)
                            if b:
                                if first_chunk_t is None:
                                    first_chunk_t = time.perf_counter()
                                total_audio += len(b)
                if sc.input_transcription and sc.input_transcription.text:
                    user_parts.append(sc.input_transcription.text)
                if sc.output_transcription and sc.output_transcription.text:
                    asst_parts.append(sc.output_transcription.text)
                if sc.turn_complete:
                    break

            t_done = time.perf_counter()
            result["first_chunk_s"] = (first_chunk_t - t_send) if first_chunk_t else None
            result["total_s"] = t_done - t_send
            result["asst_txt"] = "".join(asst_parts).strip()
            result["user_txt"] = "".join(user_parts).strip()
            result["audio_bytes"] = total_audio
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    return result


async def main():
    wav_arg = sys.argv[1] if len(sys.argv) > 1 else None
    if wav_arg:
        wav_path = Path(wav_arg)
    else:
        # Default: the archived Hebrew TTS sample
        for cand in ["bench_input_he.wav", "turn_001.wav"]:
            p = OUT_DIR / cand
            if p.exists():
                wav_path = p
                break
        else:
            print("No WAV found. Pass one as argv[1] or create archive/bench_input_he.wav "
                  "(via tools/make_he_sample.py).")
            return 1

    print(f"Using audio: {wav_path}")
    audio = load_audio_as_16k_int16(wav_path)
    print(f"Loaded {len(audio)/16000:.2f}s @ 16k mono int16")

    client = genai.Client(api_key=get_api_key())

    # (model, use_lang_code). Native-audio models reject he-IL — try without.
    models_to_test = [
        ("gemini-3.1-flash-live-preview", True),
        ("gemini-2.5-flash-native-audio-preview-12-2025", False),
        ("gemini-2.5-flash-native-audio-preview-09-2025", False),
        ("gemini-2.5-flash-native-audio-latest", False),
    ]

    results = []
    for m, lang in models_to_test:
        # Run twice and keep the second (warmer) measurement — handshake/region
        # warmup adds noise to the first hit.
        for trial in range(2):
            print(f"\n=== {m} (lang_code={'he-IL' if lang else 'none'}) trial {trial+1} ===")
            r = await benchmark_one(client, m, audio, use_lang_code=lang)
            r["trial"] = trial + 1
            results.append(r)
            if r["error"]:
                print(f"  ERROR: {r['error']}")
                break  # don't retry on hard error
            else:
                print(f"  first_chunk={r['first_chunk_s']:.3f}s  total={r['total_s']:.3f}s  audio_bytes={r['audio_bytes']}")
                print(f"  user={r['user_txt']!r}")
                print(f"  asst={r['asst_txt']!r}")
        if r["error"]:
            print(f"  ERROR: {r['error']}")
        else:
            print(f"  first_chunk={r['first_chunk_s']:.3f}s  total={r['total_s']:.3f}s  audio_bytes={r['audio_bytes']}")
            print(f"  user={r['user_txt']!r}")
            print(f"  asst={r['asst_txt']!r}")

    out_json = OUT_DIR / "model_benchmark.json"
    out_json.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {out_json}")

    # Write a small markdown report
    md = ["# Model benchmark", "",
          f"Test audio: `{wav_path.name}` ({len(audio)/16000:.2f}s)",
          f"Voice: Aoede  |  Language code: he-IL",
          "",
          "| model | first_chunk (s) | total (s) | audio bytes | error | user_txt | asst_txt |",
          "|---|---|---|---|---|---|---|"]
    for r in results:
        md.append(
            f"| `{r['model']}` | "
            f"{r['first_chunk_s']:.3f}" if r['first_chunk_s'] else f"| `{r['model']}` | n/a"
        )
    md = ["# Model benchmark", "",
          f"- Test audio: `{wav_path.name}` ({len(audio)/16000:.2f}s) — Hebrew TTS sample",
          f"- Voice: Aoede",
          f"- System prompt: {SYSTEM_PROMPT_HE!r}",
          "",
          "Each model was hit twice; the second hit is the warm number.",
          "",
          "| Model | lang_code | trial | First chunk (s) | Total (s) | Audio bytes | Error |",
          "|---|---|---:|---:|---:|---:|---|"]
    for r in results:
        fc = f"{r['first_chunk_s']:.3f}" if r["first_chunk_s"] else "—"
        tt = f"{r['total_s']:.3f}" if r["total_s"] else "—"
        ab = str(r["audio_bytes"]) if r["audio_bytes"] else "—"
        err = (r.get("error") or "").replace("|", "\\|")
        md.append(f"| `{r['model']}` | {r.get('lang_code') or '—'} | {r.get('trial','?')} | {fc} | {tt} | {ab} | {err} |")
    md.append("")
    md.append("## Transcriptions")
    for r in results:
        if r["error"]:
            continue
        md.append(f"### `{r['model']}` (trial {r.get('trial','?')})")
        md.append(f"- user (input transcription): {r['user_txt']!r}")
        md.append(f"- asst (output transcription): {r['asst_txt']!r}")
        md.append("")
    (OUT_DIR / "model_benchmark.md").write_text("\n".join(md), encoding="utf-8")
    print(f"Wrote {OUT_DIR / 'model_benchmark.md'}")


if __name__ == "__main__":
    asyncio.run(main())
