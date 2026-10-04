#!/usr/bin/env python3
"""Run the whole dashboard with no robot: the laptop's speakers are the robot.

    .venv\\Scripts\\python.exe tools\\dry_run.py
        The real dashboard on http://127.0.0.1:8765, your real microphone,
        the robot's voice out of the laptop speakers. Everything else --
        the backend picker, Gemini / GPT-Live / ElevenLabs, the turn loop,
        transcripts, conversations/<timestamp>/ -- is exactly what runs with
        the robot. Motion commands are printed instead of performed.

    .venv\\Scripts\\python.exe tools\\dry_run.py --mic-wav a.wav --mic-wav b.wav
        The microphone is replaced too: each capture plays the next WAV (16
        kHz mono) in real time, then silence. With --mute, nothing is played
        either. This is how a full conversation is tested unattended.

What it proves: the code path from the mic to the robot's audio frames, for
every backend. What it cannot: the robot itself (its speaker, its motion,
the SSH hop), which is why it exists only as a rehearsal.
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import numpy as np

import conversation as conv_mod
import system as system_mod


class LaptopRobot:
    """StreamingRobotPlayer's interface, played on the laptop."""

    def __init__(self, host="", user="", password="", robot_log_path=None,
                 mute: bool = False):
        self.init_time_s = 0.0
        self._mute = mute
        self._buf: "queue.Queue[np.ndarray]" = queue.Queue()
        self._closed = False
        self.motions: list = []
        self._stream = None
        if not mute:
            import sounddevice as sd
            self._pending = np.zeros(0, dtype=np.float32)

            def cb(outdata, frames, time_info, status):
                while self._pending.size < frames:
                    try:
                        nxt = self._buf.get_nowait()
                    except queue.Empty:
                        break
                    self._pending = np.concatenate([self._pending, nxt])
                n = min(frames, self._pending.size)
                outdata[:n, 0] = self._pending[:n]
                outdata[n:, 0] = 0.0
                self._pending = self._pending[n:]

            self._stream = sd.OutputStream(samplerate=conv_mod.GEMINI_OUTPUT_RATE,
                                           channels=1, dtype="float32",
                                           callback=cb)
            self._stream.start()
        conv_mod.log_ssh.info("dry run: the laptop is the robot (%s)",
                              "muted" if mute else "speakers")

    @property
    def connected(self) -> bool:
        return not self._closed

    def stream_chunk(self, samples_int16_24k: np.ndarray) -> bool:
        if samples_int16_24k.size == 0:
            return False
        if not self._mute:
            self._buf.put(samples_int16_24k.astype(np.float32) / 32768.0)
        conv_mod._emit("transport.audio.sent", bytes=int(samples_int16_24k.size * 2),
                       samples=int(samples_int16_24k.size))
        return True

    def end_turn(self) -> None:
        conv_mod._emit("transport.sentinel.sent", sentinel_name="TURN_END",
                       sentinel_hex="0x00000000")

    def clear_playback(self) -> None:
        with self._buf.mutex:
            self._buf.queue.clear()

    def signal_listening_start(self) -> None:
        pass

    def signal_listening_end(self) -> None:
        pass

    def send_motion_command(self, cmd: dict) -> None:
        self.motions.append(cmd)
        conv_mod.log_motion.info("dry run motion: %s", json.dumps(cmd))

    def close(self) -> None:
        self._closed = True
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass


class WavMic:
    """Stands in for sounddevice.InputStream: each capture plays the next WAV
    in real time, then silence until the capture closes."""

    script: list = []
    _next = 0
    _lock = threading.Lock()

    def __init__(self, samplerate, channels, dtype, blocksize, device, callback):
        self.rate = int(samplerate)
        self.block = int(blocksize)
        self.callback = callback
        with WavMic._lock:
            idx = WavMic._next
            WavMic._next += 1
        lead = np.zeros(int(0.8 * self.rate), dtype=np.int16)
        if idx < len(WavMic.script):
            self.audio = np.concatenate([lead, WavMic.script[idx]])
            print("[dry run] mic plays {} ({:.1f}s)".format(
                WavMic.names[idx], WavMic.script[idx].size / self.rate), flush=True)
        else:
            self.audio = np.zeros(0, dtype=np.int16)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        pos = 0
        t0 = time.perf_counter()
        sent = 0
        while not self._stop.is_set():
            block = self.audio[pos:pos + self.block]
            pos += self.block
            if block.size < self.block:
                block = np.concatenate([block, np.zeros(self.block - block.size, np.int16)])
            self.callback(block.reshape(-1, 1), self.block, None, None)
            sent += self.block
            due = t0 + sent / self.rate
            time.sleep(max(0.0, due - time.perf_counter()))

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=1.0)
        return False


def _load_wav_16k(path: str) -> np.ndarray:
    import soundfile as sf
    from scipy.signal import resample_poly
    from math import gcd
    audio, sr = sf.read(path, dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio[:, 0]
    if sr != 16000:
        g = gcd(sr, 16000)
        audio = resample_poly(audio, 16000 // g, sr // g)
    return np.clip(audio * 32768.0, -32768, 32767).astype(np.int16)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mic-wav", action="append", default=[],
                    help="replace the microphone: one WAV per turn, in order")
    ap.add_argument("--mute", action="store_true",
                    help="do not play the robot's voice on the speakers")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args, rest = ap.parse_known_args()

    mute = args.mute

    def _robot_factory(host, user, password, robot_log_path=None):
        return LaptopRobot(host, user, password, robot_log_path, mute=mute)

    system_mod.StreamingRobotPlayer = _robot_factory
    conv_mod.StreamingRobotPlayer = _robot_factory

    if args.mic_wav:
        WavMic.script = [_load_wav_16k(p) for p in args.mic_wav]
        WavMic.names = [Path(p).name for p in args.mic_wav]
        conv_mod.sd.InputStream = WavMic
        # Device resolution would still go looking for the K11; the WAV mic
        # runs at 16 kHz on the laptop path whatever is plugged in.
        conv_mod._INPUT_DEVICE_INFO = conv_mod._default_input_info()
        conv_mod._INPUT_DEVICE_INFO["name"] = "dry-run WAV mic"

    import laptop_chat
    argv = ["--port", str(args.port), "--robot-host", "127.0.0.1"] + rest
    if args.no_browser:
        argv.append("--no-browser")
    return laptop_chat.main(argv)


if __name__ == "__main__":
    sys.exit(main())
