"""Live-mic smoke: open InputStream at 16k mono int16 for 1 second and
report whether we got the expected ~50 frames of 320 samples.
Doesn't require the user to speak.
"""
import sys, time, queue
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import sounddevice as sd
import webrtcvad
import numpy as np

frame_samples = 320  # 20ms @ 16k
q: queue.Queue = queue.Queue()
def cb(indata, frames, time_info, status):
    if status:
        print("status:", status)
    q.put(indata.copy())

print("Opening InputStream 16k mono int16 blocksize=320...")
with sd.InputStream(samplerate=16000, channels=1, dtype="int16",
                    blocksize=frame_samples, callback=cb):
    t0 = time.perf_counter()
    frames_got = 0
    samples_got = 0
    while time.perf_counter() - t0 < 1.0:
        try:
            f = q.get(timeout=0.2)
            frames_got += 1
            samples_got += len(f)
        except queue.Empty:
            pass
print(f"Got {frames_got} callbacks, {samples_got} samples in 1.0s")
print(f"Expected ~50 callbacks, ~16000 samples; rate seen: {samples_got/1.0:.0f} Hz")

# Run a few of those frames through VAD just to prove the byte layout is right.
v = webrtcvad.Vad(2)
ok = 0
fail = 0
# Re-create stream to grab fresh data and run VAD inline
with sd.InputStream(samplerate=16000, channels=1, dtype="int16",
                    blocksize=frame_samples, callback=cb):
    while not q.empty():
        q.get_nowait()
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 0.5:
        try:
            f = q.get(timeout=0.2)
        except queue.Empty:
            continue
        # f shape: (N, 1)
        arr = f[:, 0] if f.ndim > 1 else f
        raw = arr.tobytes()
        for off in range(0, len(raw) - 640 + 1, 640):
            try:
                v.is_speech(raw[off:off+640], 16000)
                ok += 1
            except Exception as e:
                fail += 1
print(f"VAD frames OK: {ok}, fail: {fail}")
