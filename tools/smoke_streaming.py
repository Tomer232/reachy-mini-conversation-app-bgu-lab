"""Phase 2 smoke test: stream two consecutive Gemini turns to the robot
through ONE long-running robot process. No microphone — feeds the
prerecorded Hebrew WAV (archive/bench_input_he.wav) twice.

This validates:
  - StreamingRobotPlayer init + ready handshake
  - Real-time streaming of Gemini's 24 kHz chunks down to the robot
  - end_turn flush + boundary log
  - Two turns through one robot process (no per-turn init)
  - Clean shutdown with rc=0

Outputs (turn WAVs, timings.csv) are written to archive/smoke_streaming/.
"""
import sys
import csv
import time
import asyncio
from dataclasses import asdict
from pathlib import Path

import numpy as np
import soundfile as sf

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import laptop_chat as m

ARCHIVE = ROOT / "archive"
SMOKE_OUT = ARCHIVE / "smoke_streaming"
SMOKE_OUT.mkdir(parents=True, exist_ok=True)


def _append_csv(t: "m.TurnTimings") -> None:
    csv_path = SMOKE_OUT / "timings.csv"
    new_file = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(t).keys()))
        if new_file:
            w.writeheader()
        w.writerow(asdict(t))


async def go():
    sample = ARCHIVE / "bench_input_he.wav"
    audio16, sr = sf.read(str(sample), dtype="int16")
    assert sr == 16000, f"bench sample is {sr}, expected 16000"
    print(f"Loaded {sample.name} ({len(audio16)/16000:.2f}s)")

    client = m.genai.Client(api_key=m.get_api_key())
    robot = m.StreamingRobotPlayer(m.ROBOT_HOST, m.ROBOT_USER, m.ROBOT_PASSWORD)
    print(f"Robot init took {robot.init_time_s:.2f}s (one-time)")
    cfg = m.build_live_config()

    try:
        async with client.aio.live.connect(model=m.GEMINI_MODEL, config=cfg) as session:
            print("Live session opened.")
            for turn in (1, 2):
                t = m.TurnTimings(turn=turn)
                t_turn = time.perf_counter()

                t_send = time.perf_counter()
                await session.send_realtime_input(
                    audio=m.types.Blob(
                        data=audio16.tobytes(),
                        mime_type=f"audio/pcm;rate={m.GEMINI_INPUT_RATE}",
                    )
                )
                await session.send_realtime_input(audio_stream_end=True)
                t_send_done = time.perf_counter()
                t.vad_to_send_s = t_send_done - t_send

                resp_audio, user_txt, asst_txt = await m.drain_one_turn_streaming(
                    session, robot, t, t_send_done
                )
                t.user_chars = len(user_txt)
                t.asst_chars = len(asst_txt)
                t.wall_clock_s = time.perf_counter() - t_turn

                if resp_audio.size:
                    out = SMOKE_OUT / f"smoke_stream_turn_{turn:03d}.wav"
                    sf.write(out, resp_audio, m.GEMINI_OUTPUT_RATE, subtype="PCM_16")

                print(f"\n[turn {turn}]")
                print(f"  YOU:   {user_txt!r}")
                print(f"  ROBOT: {asst_txt!r}")
                print(f"  {t.log_line()}")
                _append_csv(t)

                # Let the robot finish playing this turn before the next
                # send (no flow control yet — we approximate with sleep).
                play_time = t.audio_duration_s
                # +0.4s safety margin for robot-side scheduling
                wait = max(0.0, play_time + 0.4 - (time.perf_counter() - t_turn))
                if wait > 0:
                    print(f"  waiting {wait:.2f}s for robot to finish playback…")
                    await asyncio.sleep(wait)
    finally:
        print("\nClosing robot channel…")
        time.sleep(0.5)
        robot.close()


asyncio.run(go())
