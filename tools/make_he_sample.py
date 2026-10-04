"""Generate a Hebrew test audio sample for benchmarking the Live API.

Uses google-genai's generate_content with a TTS-capable model to produce a
short Hebrew utterance. Saves to archive/bench_input_he.wav at 16 kHz.
"""
import sys
import wave
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

from google import genai
from google.genai import types

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "archive"
OUT_DIR.mkdir(exist_ok=True)

HEBREW_UTTERANCE = "שלום ראצ׳י, אתה יכול לספר לי בדיחה קצרה?"


def get_api_key() -> str:
    import os
    k = os.environ.get("GEMINI_API_KEY")
    if k:
        return k.strip()
    p = ROOT / ".gemini_key"
    if p.exists():
        return p.read_text().strip()
    return (ROOT.parent / "reachy-mini llm gemini token.txt").read_text().strip()


def synthesize_with_tts(client) -> bytes | None:
    """Try Gemini's TTS model. Returns raw PCM 24k mono int16 bytes, or None on failure."""
    # Common TTS model IDs to try
    tts_models = [
        "gemini-2.5-flash-preview-tts",
        "gemini-2.5-pro-preview-tts",
        "gemini-2.5-flash-tts",
    ]
    for m in tts_models:
        try:
            print(f"Trying TTS model: {m}")
            resp = client.models.generate_content(
                model=m,
                contents=f"Say in a friendly female voice: {HEBREW_UTTERANCE}",
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        language_code="he-IL",
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Aoede")
                        ),
                    ),
                ),
            )
            # Extract PCM bytes
            for cand in resp.candidates or []:
                for part in cand.content.parts or []:
                    if part.inline_data and part.inline_data.data:
                        data = part.inline_data.data
                        if isinstance(data, str):
                            import base64
                            data = base64.b64decode(data)
                        print(f"  got {len(data)} audio bytes via {m}")
                        return data
        except Exception as e:
            print(f"  {m} failed: {type(e).__name__}: {e}")
    return None


def synthesize_with_live(client) -> bytes | None:
    """Fallback: use the Live API itself to synthesize Hebrew speech."""
    import asyncio, base64
    async def _go():
        cfg = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                language_code="he-IL",
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Aoede")
                ),
            ),
            system_instruction="Repeat the user message exactly, in Hebrew, with no extra words.",
        )
        chunks = []
        async with client.aio.live.connect(
            model="gemini-3.1-flash-live-preview", config=cfg
        ) as session:
            await session.send_client_content(
                turns=[types.Content(role="user", parts=[types.Part(text=HEBREW_UTTERANCE)])],
                turn_complete=True,
            )
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
                                chunks.append(b)
                if sc.turn_complete:
                    break
        return b"".join(chunks) if chunks else None
    return asyncio.run(_go())


def main():
    client = genai.Client(api_key=get_api_key())

    audio_24k = synthesize_with_tts(client)
    if not audio_24k:
        print("TTS model path failed. Falling back to Live API echo...")
        audio_24k = synthesize_with_live(client)
        if not audio_24k:
            print("Both paths failed. Bail.")
            return 1

    # Save as 24 kHz first (the rate Live emits)
    arr24 = np.frombuffer(audio_24k, dtype=np.int16)
    sf.write(OUT_DIR / "bench_input_he_24k.wav", arr24, 24000, subtype="PCM_16")
    print(f"Wrote {OUT_DIR/'bench_input_he_24k.wav'} ({len(arr24)/24000:.2f}s)")

    # Resample to 16 kHz for Live API input
    from math import gcd
    g = gcd(24000, 16000)
    up = 16000 // g
    down = 24000 // g
    f = arr24.astype(np.float32) / 32768.0
    f16 = resample_poly(f, up, down)
    f16 = np.clip(f16, -1.0, 1.0)
    arr16 = (f16 * 32767).astype(np.int16)
    sf.write(OUT_DIR / "bench_input_he.wav", arr16, 16000, subtype="PCM_16")
    print(f"Wrote {OUT_DIR/'bench_input_he.wav'} ({len(arr16)/16000:.2f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
