#!/usr/bin/env python3
"""Synthesize the spoken test turns tools/check_backends.py plays as the mic.

Writes tools/test_audio/{he,en}_{1,2,3}.wav at 16 kHz mono: a question, a
follow-up, and a goodbye (which exercises the end-phrase path); and
{he,en}_see.wav, a question about what the camera sees. Uses Gemini
TTS with the Gemini key, so it needs no other provider. Run once; the files
are small and checked in, so a fresh checkout has them without a key.
"""

from __future__ import annotations

import sys
import time
from math import gcd
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

OUT = ROOT / "tools" / "test_audio"
TTS_MODEL = "gemini-3.8-flash-tts"

PHRASES = {
    "he_1": "שלום ריצ'י, אתה יכול לספר לי בדיחה קצרה?",
    "he_2": "חחח, יפה. מה אתה הכי אוהב לעשות?",
    "he_3": "תודה רבה, להתראות!",
    "en_1": "Hi Reachy, can you tell me a short joke?",
    "en_2": "Ha, nice one. What do you like doing most?",
    "en_3": "Thanks a lot, goodbye!",
    # Camera vision (vision.py): asked with a picture standing in for the
    # camera (REACHY_CAMERA_IMAGE).
    "he_see": "ריצ'י, אתה רואה אותי? מה אני לובשת ומה אני מחזיקה?",
    "en_see": "Reachy, can you see me? What am I wearing, and what am I holding?",
}


def main() -> int:
    from google import genai
    from google.genai import types
    import credentials

    key = credentials.resolve("gemini").key
    client = genai.Client(api_key=key)
    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in PHRASES.items():
        path = OUT / (name + ".wav")
        if path.exists() and "--force" not in sys.argv:
            print("keep", path.name)
            continue
        resp = client.models.generate_content(
            model=TTS_MODEL,
            contents=text,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name="Puck"))),
            ),
        )
        pcm = resp.candidates[0].content.parts[0].inline_data.data
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        g = gcd(24000, 16000)
        audio = resample_poly(audio, 16000 // g, 24000 // g)
        sf.write(path, np.clip(audio, -1, 1), 16000, subtype="PCM_16")
        print("wrote {} ({:.1f}s): {}".format(path.name, len(audio) / 16000, text))
        time.sleep(1.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
