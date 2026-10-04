#!/usr/bin/env python3
"""Robot-side check that the local (subprocess) transport works.

The counterpart to `test_streaming_robot.py`, which does the same thing over
SSH from the laptop. This one runs ON the robot and drives
`robot_streaming_player.py` as a child process — no Gemini, no microphone,
just: can we start it, does it say ready, does audio reach the speaker, does
it shut down cleanly on stdin EOF.

    ssh pollen@<robot>
    cd /home/pollen/reachy_chat
    /venvs/mini_daemon/bin/python tools/test_local_player.py

You should hear a short two-tone beep and see the robot keep breathing.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import conversation as conv  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("test.local_player")

TONE_S = 0.6
RATE = conv.GEMINI_OUTPUT_RATE   # feed_audio expects Gemini's 24 kHz int16


def tone(freq: float, seconds: float) -> np.ndarray:
    t = np.arange(int(seconds * RATE), dtype=np.float32) / RATE
    # Fade the edges so the click at the boundary isn't mistaken for the
    # chunk-boundary artifacts we actually care about hearing.
    env = np.minimum(1.0, np.minimum(t, seconds - t) * 40.0)
    return (0.25 * env * np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16)


def main() -> int:
    conv.LOCAL_ROBOT = True
    log.info("LOCAL_ROBOT=%s, player=%s",
             conv.LOCAL_ROBOT, conv.ROBOT_STREAMING_PLAYER)

    t0 = time.perf_counter()
    player = conv.StreamingRobotPlayer("", "", "", None)
    log.info("player ready in %.2fs (init_time_s=%.2f)",
             time.perf_counter() - t0, player.init_time_s)

    try:
        log.info("connected=%s", player.connected)

        log.info("sending two tones…")
        for freq in (440.0, 660.0):
            player.stream_chunk(tone(freq, TONE_S))
        player.end_turn()

        # Let the audio actually play out before we tear down; end_turn only
        # marks the boundary, it does not block on playback.
        time.sleep(2 * TONE_S + 1.0)

        log.info("sending a motion command (head left)…")
        player.send_motion_command({"type": "head", "direction": "left"})
        time.sleep(1.5)
        player.send_motion_command({"type": "stop"})
        time.sleep(0.5)

        log.info("connected=%s (expect True)", player.connected)
    finally:
        log.info("closing…")
        player.close()

    log.info("closed. If you heard two beeps and saw the head move, the local "
             "transport is working.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
