"""End-to-end smoke test of laptop_chat without using the mic.

PHASE-1 SMOKE — targets the original RobotPlayer.upload/play API and
laptop_chat's old module-level OUT_DIR. Both have been removed in
Phase 2 (streaming pipeline), so this script will raise AttributeError
against the current laptop_chat.py. Kept for historical reference; for
the current architecture see smoke_streaming.py.

Feeds a prerecorded Hebrew WAV twice through the SAME live session,
prints timings, and pushes the resulting audio to the robot.

Does NOT touch the laptop microphone.
"""
import sys, asyncio, base64, time
from pathlib import Path
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import laptop_chat as m

async def go():
    # Load Hebrew sample
    sample = m.OUT_DIR / "bench_input_he.wav"
    audio, sr = sf.read(str(sample), dtype="int16")
    assert sr == 16000, f"sample is {sr}, expected 16000"
    print(f"Loaded {sample.name} ({len(audio)/16000:.2f}s)")

    client = m.genai.Client(api_key=m.get_api_key())
    # If --no-robot is passed (or env SMOKE_NO_ROBOT=1), skip the robot leg.
    import os
    skip_robot = ("--no-robot" in sys.argv) or os.environ.get("SMOKE_NO_ROBOT") == "1"
    robot = None if skip_robot else m.RobotPlayer(m.ROBOT_HOST, m.ROBOT_USER, m.ROBOT_PASSWORD)
    cfg = m.build_live_config()

    try:
        async with client.aio.live.connect(model=m.GEMINI_MODEL, config=cfg) as session:
            print("Live session opened.")
            for turn in (1, 2):
                t = m.TurnTimings(turn=turn)
                t_turn = time.perf_counter()

                # 1) send
                t_send = time.perf_counter()
                await session.send_realtime_input(
                    audio=m.types.Blob(
                        data=audio.tobytes(),
                        mime_type=f"audio/pcm;rate={m.GEMINI_INPUT_RATE}",
                    )
                )
                await session.send_realtime_input(audio_stream_end=True)
                t_send_done = time.perf_counter()
                t.vad_to_send_s = t_send_done - t_send

                # 2) drain
                resp_audio, user_txt, asst_txt = await m.drain_one_turn(
                    session, t, t_send_done
                )
                t.user_chars = len(user_txt)
                t.asst_chars = len(asst_txt)
                print(f"\n[turn {turn}]")
                print(f"  YOU:   {user_txt!r}")
                print(f"  ROBOT: {asst_txt!r}")

                # 3) save + (optionally) upload + play
                local = m.OUT_DIR / f"smoke_turn_{turn:03d}.wav"
                sf.write(local, resp_audio, m.GEMINI_OUTPUT_RATE, subtype="PCM_16")
                if robot is not None:
                    t_up = time.perf_counter()
                    robot.upload(str(local))
                    t.sftp_upload_s = time.perf_counter() - t_up
                    t_play = time.perf_counter()
                    robot.play()
                    t.robot_play_s = time.perf_counter() - t_play
                t.wall_clock_s = time.perf_counter() - t_turn
                print("  " + t.log_line())
                m.append_timings_csv(t)
    finally:
        if robot is not None:
            robot.close()

asyncio.run(go())
