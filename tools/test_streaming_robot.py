"""Standalone test of robot_streaming_player.py via SSH stdin streaming.

Plays two sine-wave 'turns' (440 Hz, then 660 Hz) through the same long-
running robot process — proves the player handles multi-turn streaming
without restarting and without per-turn init.

No Gemini, no laptop mic. Pure plumbing test.
"""
import sys
import time
import struct
import threading
from pathlib import Path

import numpy as np
import paramiko

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROBOT_HOST = "10.100.102.18"
ROBOT_USER = "pollen"
ROBOT_PASSWORD = "root"
ROBOT_PYTHON = "/venvs/mini_daemon/bin/python"
ROBOT_SCRIPT = "/home/pollen/scripts/robot_streaming_player.py"

SR = 16000                     # robot output rate
TONE_DURATION_S = 2.0
CHUNK_SAMPLES = 512            # ~32 ms per chunk
INTER_TURN_PAUSE_S = 1.0

HEADER_LEN = 4
TURN_END = struct.pack(">I", 0)


def make_sine(freq_hz: float, duration_s: float, sr: int = SR,
              amplitude: float = 0.5, fade_ms: float = 5.0) -> np.ndarray:
    """float32 mono sine with a short fade-in/out to avoid clicks."""
    n = int(duration_s * sr)
    t = np.arange(n, dtype=np.float32) / sr
    wave = amplitude * np.sin(2 * np.pi * freq_hz * t).astype(np.float32)
    fade_n = max(1, int(fade_ms * sr / 1000))
    ramp = np.linspace(0.0, 1.0, fade_n, dtype=np.float32)
    wave[:fade_n] *= ramp
    wave[-fade_n:] *= ramp[::-1]
    return wave


def frame(payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + payload


def stream_drainer(channel: paramiko.Channel, ready_event: threading.Event,
                   stderr_lines: list[str], stop: threading.Event) -> None:
    """Drain stderr from the robot, log lines, set ready_event on 'ready'."""
    buf = b""
    while not stop.is_set():
        if channel.recv_stderr_ready():
            chunk = channel.recv_stderr(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                s = line.decode("utf-8", errors="replace").rstrip()
                if not s:
                    continue
                stderr_lines.append(s)
                print(f"  [robot stderr] {s}")
                if s == "ready":
                    ready_event.set()
        elif channel.exit_status_ready():
            # consume any tail
            tail = channel.recv_stderr(4096)
            if tail:
                buf += tail
                continue
            break
        else:
            time.sleep(0.02)


def main() -> int:
    print(f"Connecting to {ROBOT_HOST}…")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(ROBOT_HOST, username=ROBOT_USER, password=ROBOT_PASSWORD, timeout=15)
    transport = c.get_transport()
    channel = transport.open_session()
    # request a pty? NO — we want raw binary stdin and clean stderr separation
    channel.exec_command(f"{ROBOT_PYTHON} -u {ROBOT_SCRIPT}")
    print("exec_command issued; waiting for 'ready'…")

    ready_event = threading.Event()
    stderr_lines: list[str] = []
    stop = threading.Event()
    drainer = threading.Thread(
        target=stream_drainer,
        args=(channel, ready_event, stderr_lines, stop),
        daemon=True,
    )
    drainer.start()

    t_start = time.perf_counter()
    if not ready_event.wait(timeout=30):
        print("ERROR: robot never said 'ready' within 30s. stderr:")
        for s in stderr_lines:
            print(f"  {s}")
        channel.close()
        c.close()
        return 1
    t_ready = time.perf_counter() - t_start
    print(f"ROBOT READY in {t_ready:.2f}s — streaming sines now")

    # --- turn 1: 440 Hz ---
    print("\n>>> turn 1: 440 Hz sine, 2.0s")
    wave1 = make_sine(440.0, TONE_DURATION_S)
    t_send = time.perf_counter()
    for off in range(0, len(wave1), CHUNK_SAMPLES):
        chunk = wave1[off:off + CHUNK_SAMPLES]
        channel.send(frame(chunk.tobytes()))
    channel.send(TURN_END)
    print(f"  sent {len(wave1)} samples in "
          f"{(len(wave1)+CHUNK_SAMPLES-1)//CHUNK_SAMPLES} chunks "
          f"({time.perf_counter()-t_send:.3f}s to enqueue)")

    # Wait long enough for the robot to actually play it (no flow control yet)
    print(f"  sleeping {TONE_DURATION_S + INTER_TURN_PAUSE_S:.1f}s for playback")
    time.sleep(TONE_DURATION_S + INTER_TURN_PAUSE_S)

    # --- turn 2: 660 Hz ---
    print(">>> turn 2: 660 Hz sine, 2.0s")
    wave2 = make_sine(660.0, TONE_DURATION_S)
    t_send = time.perf_counter()
    for off in range(0, len(wave2), CHUNK_SAMPLES):
        chunk = wave2[off:off + CHUNK_SAMPLES]
        channel.send(frame(chunk.tobytes()))
    channel.send(TURN_END)
    print(f"  sent {len(wave2)} samples ({time.perf_counter()-t_send:.3f}s to enqueue)")

    # Wait, then close
    time.sleep(TONE_DURATION_S + 0.5)
    print("\nsending EOF to robot…")
    channel.shutdown_write()

    rc = channel.recv_exit_status()
    stop.set()
    drainer.join(timeout=2)
    print(f"robot exited rc={rc}")
    c.close()
    print("\n--- robot stderr log ---")
    for s in stderr_lines:
        print(f"  {s}")
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
