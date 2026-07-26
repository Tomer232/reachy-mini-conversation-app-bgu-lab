"""Lavalier smoke test: open the resolved INPUT_DEVICE at 16000/1ch for 3 s,
print RMS and peak, and save a wav to a temp path.

Use this to confirm the K11 receiver is paired, powered, unmuted, and in
range — independent of VAD, Gemini, and the robot. Resolution uses the same
conversation._ensure_input_device() helper the live pipeline uses, so the
device picked here is exactly the one a conversation would open.

Run from the project root:

    .venv\\Scripts\\python.exe tools\\mic_check.py
    .venv\\Scripts\\python.exe tools\\mic_check.py --seconds 5
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

# Make the project root importable (mirrors how other tools/*.py do it).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import sounddevice as sd
import soundfile as sf

import conversation as conv


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="K11 lavalier smoke test.")
    ap.add_argument("--seconds", type=float, default=3.0,
                    help="Capture duration in seconds (default 3.0).")
    ap.add_argument("--out", type=str, default=None,
                    help="WAV output path (default: %TEMP%/mic_check_<ts>.wav).")
    args = ap.parse_args()

    info = conv._ensure_input_device()
    print(f"INPUT_DEVICE constant: {conv.INPUT_DEVICE!r}")
    print(f"Resolved device      : name={info['name']!r} idx={info['device']} "
          f"host={info['host_api']} rate={info['rate']} channels={info['channels']}")

    rate = conv.GEMINI_INPUT_RATE  # 16000
    duration = float(args.seconds)
    n_frames = int(rate * duration)

    print(f"Capturing {duration:.1f}s at {rate} Hz mono int16…")
    t0 = time.perf_counter()
    audio = sd.rec(
        n_frames,
        samplerate=rate,
        channels=1,
        dtype="int16",
        device=info["device"],
    )
    sd.wait()
    elapsed = time.perf_counter() - t0
    audio = np.ascontiguousarray(audio[:, 0]) if audio.ndim > 1 else audio
    assert audio.dtype == np.int16

    # RMS and peak in dBFS (full-scale int16 = 32768).
    full_scale = 32768.0
    peak_abs = int(np.max(np.abs(audio))) if audio.size else 0
    peak_dbfs = 20 * np.log10(peak_abs / full_scale) if peak_abs > 0 else float("-inf")
    # Use float64 for the squared sum to avoid int16 overflow.
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2))) if audio.size else 0.0
    rms_dbfs = 20 * np.log10(rms / full_scale) if rms > 0 else float("-inf")

    print(f"  elapsed     : {elapsed:.2f}s")
    print(f"  samples     : {audio.size}")
    print(f"  peak (abs)  : {peak_abs} ({peak_dbfs:+.1f} dBFS)")
    print(f"  rms         : {rms:7.1f} ({rms_dbfs:+.1f} dBFS)")

    # Guidance — purely cosmetic.
    if peak_abs == 0:
        print("  verdict     : SILENCE — no signal at all. Receiver off, "
              "muted, wrong device, or driver issue.")
    elif rms_dbfs < -50:
        print("  verdict     : very quiet — receiver may be muted, far from "
              "mouth, or gain too low.")
    elif peak_dbfs > -1.0:
        print("  verdict     : clipping — reduce gain on the receiver.")
    else:
        print("  verdict     : signal present. Compare to a built-in-mic "
              "baseline to judge gain.")

    if args.out is not None:
        out_path = Path(args.out).resolve()
    else:
        ts = time.strftime("%Y%m%d_%H%M%S")
        out_path = Path(tempfile.gettempdir()) / f"mic_check_{ts}.wav"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(out_path, audio, rate, subtype="PCM_16")
    print(f"  wav         : {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
