#!/usr/bin/env python3
"""Measure a robot's built-in microphone. Run this ON the robot.

Everything about the fleet's audio path is currently a guess, because the
built-in mic has never been measured. The K11 lavalier it replaces was
`Composite` at 48 kHz on ALSA card 3 (verified 2026-07-27), and there is no
reason to think the internal mic matches either value.

Two questions, and the second is the one that decides things:

  **1. What is it, and what will it open at?** Which device name, which rates,
  how many channels. Prints the exact `--mic-match` and `--mic-rate` to launch
  with, so nobody edits a constant and redeploys it to ten robots.

  **2. How loudly does the robot hear itself?** The lavalier was clipped to
  the *person*; the built-in mic sits in the head, inches from the robot's own
  speaker and inside its servos. That changes two things that have already
  cost this project a demo:

    - *Noise turns.* A single VAD frame over 0.5 starts a turn, and the 0.8 s
      hangover guarantees it clears MIN_SPEECH_S, so a second of room tone goes
      to the model as a question. Servo noise reaching the mic directly makes
      this more frequent, and no threshold can fix it -- real one-word Hebrew
      answers and noise overlap completely on frame count and probability.
    - *Barge-in.* GPT-LIVE-MIGRATION-PLAN.md 4.3 needs the echo floor to know
      whether a threshold gate is viable at all. Margin of roughly 12 dB or
      more between speech and echo means the cheap option works. Less means
      AEC, which is only feasible because capture and playback now share a
      machine.

    python tools/probe_internal_mic.py                 # devices and rates
    python tools/probe_internal_mic.py --echo          # also the echo floor
    python tools/probe_internal_mic.py --seconds 5

The echo test needs the robot to make noise. It does not speak by itself --
it tells you when to trigger a sound and measures what comes back, so it works
whether you play a cue from the show board or just talk to the robot.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import numpy as np
    import sounddevice as sd
except Exception as exc:  # noqa: BLE001
    raise SystemExit(
        "this needs numpy and sounddevice, which the robot's venv has: run it "
        "with /venvs/mini_daemon/bin/python ({})".format(exc))

# The rates worth trying, commonest first. 16000 is what the pipeline wants;
# anything else means a decimation step (48000 -> 16000 is the clean x3).
CANDIDATE_RATES = [16000, 48000, 44100, 32000, 24000, 8000]

# What the rest of the app captures at.
TARGET_RATE = 16000


def _dbfs(x) -> float:
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    return 20.0 * math.log10(peak) if peak > 1e-9 else -120.0


def list_inputs() -> list:
    devices = sd.query_devices()
    hostapis = sd.query_hostapis()
    rows = []
    for i, d in enumerate(devices):
        if d.get("max_input_channels", 0) <= 0:
            continue
        hi = d.get("hostapi", -1)
        rows.append({
            "index": i,
            "name": d.get("name", ""),
            "host": hostapis[hi]["name"] if 0 <= hi < len(hostapis) else "?",
            "channels": d.get("max_input_channels", 0),
            "default_rate": int(d.get("default_samplerate", 0) or 0),
        })
    return rows


def accepted_rates(index: int) -> list:
    ok = []
    for rate in CANDIDATE_RATES:
        try:
            sd.check_input_settings(device=index, channels=1, samplerate=rate)
            ok.append(rate)
        except Exception:  # noqa: BLE001
            continue
    return ok


def measure(index: int, rate: int, seconds: float, label: str) -> dict:
    """Record and report levels. Peak matters more than RMS here: the VAD
    triggers on transients, and it is a transient that starts a junk turn."""
    print("    recording {:.1f}s...".format(seconds), end="", flush=True)
    try:
        audio = sd.rec(int(seconds * rate), samplerate=rate, channels=1,
                       dtype="float32", device=index)
        sd.wait()
    except Exception as exc:  # noqa: BLE001
        print(" failed: {}".format(exc))
        return {}
    audio = audio.reshape(-1)
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    rms = float(np.sqrt(np.mean(audio ** 2))) if len(audio) else 0.0
    print(" peak {:.4f} ({:.1f} dBFS)   rms {:.4f} ({:.1f} dBFS)".format(
        peak, _dbfs(audio), rms,
        20 * math.log10(rms) if rms > 1e-9 else -120.0))
    if peak < 1e-6:
        # The exact trap the K11 set: a device that enumerates and opens
        # perfectly while delivering digital silence.
        print("    WARNING: digital silence. The device opened and gave "
              "nothing at all -- muted, or not the device you think it is.")
    return {"label": label, "peak": peak, "rms": rms,
            "peak_dbfs": _dbfs(audio), "n": len(audio)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seconds", type=float, default=4.0,
                    help="how long each measurement records (default 4)")
    ap.add_argument("--device", type=int, default=None,
                    help="skip the search and measure this device index")
    ap.add_argument("--echo", action="store_true",
                    help="also measure the echo floor, which needs the robot "
                         "to make a sound while this listens")
    args = ap.parse_args()

    print("=" * 68)
    print("INPUT DEVICES")
    print("=" * 68)
    rows = list_inputs()
    if not rows:
        print("no capture devices at all. On the robot, check `arecord -l`.")
        return 1
    for r in rows:
        print("  [{index}] {name}".format(**r))
        print("       host={host}  channels={channels}  "
              "default={default_rate} Hz".format(**r))

    candidates = ([r for r in rows if r["index"] == args.device]
                  if args.device is not None else rows)
    if not candidates:
        print("\nno device with index {}".format(args.device))
        return 1

    print()
    print("=" * 68)
    print("WHAT EACH ONE WILL OPEN AT")
    print("=" * 68)
    usable = []
    for r in candidates:
        rates = accepted_rates(r["index"])
        print("  [{}] {}".format(r["index"], r["name"]))
        print("       accepts: {}".format(
            ", ".join(str(x) for x in rates) if rates else "nothing tried"))
        if rates:
            usable.append((r, rates))

    if not usable:
        print("\nNothing opened at any rate. That is the PaErrorCode -9997 "
              "case: the device exposes only its own native rate through raw "
              "ALSA with no plug layer.")
        return 1

    print()
    print("=" * 68)
    print("LEVELS  --  speak at a normal distance during each recording")
    print("=" * 68)
    results = []
    for r, rates in usable:
        rate = TARGET_RATE if TARGET_RATE in rates else rates[0]
        print("  [{}] {}  @ {} Hz".format(r["index"], r["name"], rate))
        input("      press Enter, then speak...")
        m = measure(r["index"], rate, args.seconds, r["name"])
        if m:
            m["index"], m["rate"] = r["index"], rate
            results.append(m)

    speaking = [m for m in results if m["peak"] > 1e-6]
    if not speaking:
        print("\nEvery device delivered silence. Nothing here can hear you.")
        return 1

    best = max(speaking, key=lambda m: m["peak"])

    echo = None
    if args.echo:
        print()
        print("=" * 68)
        print("ECHO FLOOR  --  how loudly this robot hears ITSELF")
        print("=" * 68)
        print("  Make the robot speak at the volume you will actually use:")
        print("  fire a cue from the show board, or run a conversation turn.")
        print("  Stay quiet yourself -- this is measuring the robot, not you.")
        input("  press Enter when the robot is ABOUT to speak...")
        echo = measure(best["index"], best["rate"], args.seconds, "echo")

    print()
    print("=" * 68)
    print("WHAT TO DO WITH THIS")
    print("=" * 68)
    print("  Launch this robot with:")
    print("    --mic-match \"{}\"".format(best["label"][:40]))
    if best["rate"] != TARGET_RATE:
        print("    --mic-rate {}".format(best["rate"]))
        if best["rate"] % TARGET_RATE:
            print("    NOTE: {} does not divide into {} cleanly, so the x3"
                  .format(best["rate"], TARGET_RATE))
            print("    decimation path does not apply -- resampling needs a "
                  "look before this robot runs.")
    else:
        print("    (no --mic-rate needed; it opens at 16 kHz directly, which "
              "means no decimation at all)")

    if best["peak"] < 0.05:
        print("\n  Levels are LOW (peak {:.3f}). The robot path has no MME "
              "boost;".format(best["peak"]))
        print("  CAPTURE_GAIN_LINUX exists for this and is currently 2.0.")

    if echo and echo.get("peak", 0) > 0:
        margin = best["peak_dbfs"] - echo["peak_dbfs"]
        print()
        print("  speech peak : {:.1f} dBFS".format(best["peak_dbfs"]))
        print("  echo peak   : {:.1f} dBFS".format(echo["peak_dbfs"]))
        print("  margin      : {:.1f} dB".format(margin))
        if margin >= 12:
            print("  -> Clean margin. A threshold gate above the echo floor is")
            print("     viable, so barge-in is reachable without AEC.")
        elif margin > 0:
            print("  -> Thin margin. A gate will either clip quiet speech or")
            print("     let the robot interrupt itself. AEC is the honest fix,")
            print("     and it is feasible now that capture and playback share")
            print("     a machine (GPT-LIVE-MIGRATION-PLAN.md 4.3).")
        else:
            print("  -> The robot hears itself LOUDER than it hears you. Any")
            print("     open-mic design will self-trigger. Half-duplex only")
            print("     until there is AEC.")
        print()
        print("  Write these numbers into GPT-LIVE-MIGRATION-PLAN.md under a")
        print("  'Phase 0 results' heading. The next person needs them.")
    elif args.echo:
        print("\n  The echo test recorded nothing -- the robot probably did "
              "not speak during the window. Worth repeating.")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        raise SystemExit(130)
