#!/usr/bin/env python3
"""Prove the ONNX VAD on the robot matches the torch VAD on the laptop.

Robot mode swaps ``vad.SileroVAD`` (torch.jit) for ``vad_onnx.SileroVADOnnx``
because the robot has no PyTorch. That swap is only safe if the two produce the
same speech probabilities — every endpointing tunable (threshold, hangover,
min-speech) was tuned against the torch numbers.

Run with no args to do both halves:

    python tools\\test_vad_parity.py

It computes torch probabilities locally, ships the audio + ONNX wrapper to the
robot, computes ONNX probabilities there, and reports the largest disagreement.
Same-decision rate is what actually matters; raw probability deltas of ~1e-3
are expected between the two runtimes and are harmless.

``--local-only`` / ``--robot-only`` split the halves if you want to debug one
side. ``--wav`` picks a different input (default: the Hebrew bench sample).
"""

from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))

DEFAULT_WAV = PROJECT_DIR / "archive" / "bench_input_he.wav"
REMOTE_DIR = "/tmp/vad_parity"
FRAME = 512
RATE = 16000
THRESHOLD = 0.5


def load_wav_16k_mono(path: Path) -> np.ndarray:
    """Read a WAV as float32 mono at 16 kHz, resampling if needed."""
    with wave.open(str(path)) as w:
        if w.getsampwidth() != 2:
            raise SystemExit(f"{path.name}: expected 16-bit PCM")
        rate = w.getframerate()
        ch = w.getnchannels()
        raw = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    x = raw.astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch)[:, 0]
    if rate != RATE:
        from scipy.signal import resample_poly
        from math import gcd
        g = gcd(rate, RATE)
        x = resample_poly(x, RATE // g, rate // g).astype(np.float32)
        print(f"  resampled {rate} -> {RATE} Hz")
    return np.ascontiguousarray(x, dtype=np.float32)


def frames_of(x: np.ndarray) -> list[np.ndarray]:
    return [x[i:i + FRAME] for i in range(0, x.size - FRAME + 1, FRAME)]


def probs_torch(x: np.ndarray) -> list[float]:
    from vad import SileroVAD
    v = SileroVAD(threshold=THRESHOLD, frame_size=FRAME, sample_rate=RATE)
    v.reset()
    return [v.feed_frame(f) for f in frames_of(x)]


def probs_onnx(x: np.ndarray) -> list[float]:
    from vad_onnx import SileroVADOnnx
    v = SileroVADOnnx(threshold=THRESHOLD, frame_size=FRAME, sample_rate=RATE)
    v.reset()
    return [v.feed_frame(f) for f in frames_of(x)]


def compare(a: list[float], b: list[float]) -> int:
    """Report agreement between two probability series. Returns an exit code."""
    n = min(len(a), len(b))
    if len(a) != len(b):
        print(f"!! frame-count mismatch: torch={len(a)} onnx={len(b)}")
    pa, pb = np.array(a[:n]), np.array(b[:n])
    delta = np.abs(pa - pb)
    da, db = pa >= THRESHOLD, pb >= THRESHOLD
    disagree = int((da != db).sum())

    # ASCII only: this prints to a cp1252 PowerShell console.
    print(f"\nframes compared      : {n}")
    print(f"max abs prob diff    : {delta.max():.6f}")
    print(f"mean abs prob diff   : {delta.mean():.6f}")
    print(f"speech frames torch  : {int(da.sum())}")
    print(f"speech frames onnx   : {int(db.sum())}")
    print(f"decision disagreement: {disagree} frame(s) "
          f"({100.0 * disagree / n:.2f}%)")

    if disagree:
        idx = np.flatnonzero(da != db)[:10]
        print("  first disagreements (frame, torch, onnx):")
        for i in idx:
            print(f"    {i:5d}  {pa[i]:.4f}  {pb[i]:.4f}")

    # Tolerance: the runtimes differ in float accumulation order, so exact
    # equality is not the bar. Flipped decisions are, since those change turns.
    ok = disagree == 0 and delta.max() < 0.05
    print("\n" + ("PASS - the ONNX VAD can stand in for the torch one."
                  if ok else
                  "FAIL - do not deploy robot mode until this is understood."))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", default=str(DEFAULT_WAV))
    ap.add_argument("--local-only", action="store_true")
    ap.add_argument("--robot-only", action="store_true")
    ap.add_argument("--robot-host", default=None)
    args = ap.parse_args()

    wav = Path(args.wav)
    if not wav.is_file():
        raise SystemExit(f"no such wav: {wav}")
    print(f"audio: {wav}")
    x = load_wav_16k_mono(wav)
    print(f"  {x.size} samples, {x.size / RATE:.2f}s, {len(frames_of(x))} frames")

    if args.robot_only:
        print("\n=== ONNX (this machine) ===")
        print(json.dumps(probs_onnx(x)))
        return 0

    print("\n=== torch (laptop) ===")
    t = probs_torch(x)
    print(f"  {len(t)} probabilities")

    if args.local_only:
        return 0

    # --- robot half -------------------------------------------------------
    import paramiko
    sys.path.insert(0, str(PROJECT_DIR))
    from conversation import (ROBOT_HOST, ROBOT_USER, ROBOT_PASSWORD,
                              ROBOT_PYTHON)
    host = args.robot_host or ROBOT_HOST

    print(f"\n=== ONNX (robot {host}) ===")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username=ROBOT_USER, password=ROBOT_PASSWORD, timeout=15)

    sftp = c.open_sftp()
    try:
        sftp.mkdir(REMOTE_DIR)
    except IOError:
        pass
    try:
        sftp.mkdir(f"{REMOTE_DIR}/models")
    except IOError:
        pass
    for src, dst in [
        (PROJECT_DIR / "vad_onnx.py", f"{REMOTE_DIR}/vad_onnx.py"),
        (PROJECT_DIR / "models" / "silero_vad.onnx",
         f"{REMOTE_DIR}/models/silero_vad.onnx"),
        (SCRIPT_DIR / "test_vad_parity.py", f"{REMOTE_DIR}/test_vad_parity.py"),
        (wav, f"{REMOTE_DIR}/input.wav"),
    ]:
        sftp.put(str(src), dst)
        print(f"  sent {src.name}")
    sftp.close()

    # The remote copy runs --robot-only, which prints a bare JSON array last.
    cmd = (f"cd {REMOTE_DIR} && {ROBOT_PYTHON} test_vad_parity.py "
           f"--robot-only --wav {REMOTE_DIR}/input.wav")
    _in, out, err = c.exec_command(cmd, timeout=300)
    stdout = out.read().decode("utf-8", "replace")
    stderr = err.read().decode("utf-8", "replace")
    rc = out.channel.recv_exit_status()
    c.close()

    if rc != 0:
        print(stdout)
        print(stderr)
        raise SystemExit(f"robot side failed rc={rc}")

    line = next((ln for ln in reversed(stdout.splitlines())
                 if ln.startswith("[")), None)
    if line is None:
        print(stdout)
        raise SystemExit("no JSON array in robot output")
    o = json.loads(line)
    print(f"  {len(o)} probabilities")

    return compare(t, o)


if __name__ == "__main__":
    raise SystemExit(main())
