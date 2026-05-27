"""Offline test of webrtcvad against the prerecorded Hebrew sample.

Confirms the VAD classifies the speech segment as speech, and pure silence
as non-speech. Also checks sounddevice can enumerate input devices.
"""
import sys
from pathlib import Path
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import numpy as np
import soundfile as sf
import webrtcvad
import sounddevice as sd

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "archive" / "bench_input_he.wav"

# 1) sounddevice can see input devices?
print("--- input devices ---")
for i, d in enumerate(sd.query_devices()):
    if d["max_input_channels"] > 0:
        print(f"  [{i}] {d['name']} ({d['max_input_channels']} ch, {d['default_samplerate']:.0f} Hz)")
print(f"  default input: {sd.default.device}")

# 2) feed Hebrew sample through VAD at 20ms frames
audio, sr = sf.read(str(SAMPLE), dtype="int16")
assert sr == 16000
frame_samples = int(16000 * 20 / 1000)  # 320
frame_bytes = frame_samples * 2

print(f"\n--- VAD on Hebrew speech sample ({len(audio)/16000:.2f}s) ---")
for agg in (0, 1, 2, 3):
    v = webrtcvad.Vad(agg)
    raw = audio.tobytes()
    speech = total = 0
    for off in range(0, len(raw) - frame_bytes + 1, frame_bytes):
        frame = raw[off:off+frame_bytes]
        total += 1
        if v.is_speech(frame, 16000):
            speech += 1
    pct = 100*speech/total if total else 0
    print(f"  aggressiveness={agg}  speech_frames={speech}/{total}  ({pct:.1f}%)")

# 3) Pure silence
print(f"\n--- VAD on pure silence (3s) ---")
silence = np.zeros(int(16000*3), dtype=np.int16)
raw_sil = silence.tobytes()
for agg in (0, 1, 2, 3):
    v = webrtcvad.Vad(agg)
    speech = total = 0
    for off in range(0, len(raw_sil) - frame_bytes + 1, frame_bytes):
        frame = raw_sil[off:off+frame_bytes]
        total += 1
        if v.is_speech(frame, 16000):
            speech += 1
    pct = 100*speech/total if total else 0
    print(f"  aggressiveness={agg}  speech_frames={speech}/{total}  ({pct:.1f}%)")

# 4) Low-level noise (simulating ambient hum)
print(f"\n--- VAD on low-level noise (3s, ~ -50 dBFS) ---")
rng = np.random.default_rng(0)
noise = (rng.normal(0, 100, int(16000*3))).astype(np.int16)
raw_n = noise.tobytes()
for agg in (0, 1, 2, 3):
    v = webrtcvad.Vad(agg)
    speech = total = 0
    for off in range(0, len(raw_n) - frame_bytes + 1, frame_bytes):
        frame = raw_n[off:off+frame_bytes]
        total += 1
        if v.is_speech(frame, 16000):
            speech += 1
    pct = 100*speech/total if total else 0
    print(f"  aggressiveness={agg}  speech_frames={speech}/{total}  ({pct:.1f}%)")
