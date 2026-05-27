#!/usr/bin/env python3
"""Robot-side helper. Plays a WAV file via the SDK's play_sound.

Called remotely by laptop_chat.py per turn. Kept dead simple on purpose.

Usage:
    /venvs/mini_daemon/bin/python ~/scripts/robot_play.py /tmp/response.wav
"""

import sys
import time
import soundfile as sf
from reachy_mini import ReachyMini


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: robot_play.py <wav_path>", file=sys.stderr)
        return 2

    wav_path = sys.argv[1]
    try:
        duration = sf.info(wav_path).duration
    except Exception as e:
        print(f"Cannot read {wav_path}: {e}", file=sys.stderr)
        return 1

    with ReachyMini() as mini:
        mini.media.play_sound(wav_path)
        # play_sound is non-blocking; wait for it to drain plus a tiny safety margin
        time.sleep(duration + 0.3)

    return 0


if __name__ == "__main__":
    sys.exit(main())
