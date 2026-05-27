"""Phase 3A — standalone breathing test.

Connects via SSH, exec's the new robot_streaming_player.py, waits for
'ready', sleeps for 30 seconds while sending NO audio (so the only motion
the robot produces is the BreathingMove primary), then sends EOF and
waits for clean exit.

While this runs, watch the robot:
  - Head should bob gently up/down on the z axis (~5 mm, 0.1 Hz)
  - Antennas should slowly sway side-to-side (~15°, 0.5 Hz)
  - After ~30s, on stdin EOF, the robot should exit rc=0 and print the
    motion-loop frequency stats line on stderr.

If you see nothing moving, or motion that snaps after 1 s and freezes,
the BreathingMove duration/evaluate logic is wrong — read the stderr
log printed at the end.
"""
import sys
import time
import threading
from pathlib import Path

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

WATCH_SECONDS = 30.0
READY_TIMEOUT_S = 30.0


def stderr_drainer(channel, ready_event, lines, stop):
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
                lines.append(s)
                print(f"  [robot] {s}")
                if s == "ready":
                    ready_event.set()
        elif channel.exit_status_ready():
            tail = channel.recv_stderr(65536)
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
    channel.exec_command(f"{ROBOT_PYTHON} -u {ROBOT_SCRIPT}")
    print("exec_command issued; waiting for 'ready'…")

    ready = threading.Event()
    lines: list[str] = []
    stop = threading.Event()
    drainer = threading.Thread(target=stderr_drainer,
                               args=(channel, ready, lines, stop),
                               daemon=True)
    drainer.start()

    t0 = time.perf_counter()
    if not ready.wait(timeout=READY_TIMEOUT_S):
        print(f"ERROR: robot never said 'ready' within {READY_TIMEOUT_S}s")
        channel.close()
        c.close()
        return 1
    print(f"ROBOT READY in {time.perf_counter() - t0:.2f}s")
    print(f"\n  >>> Watching for {WATCH_SECONDS:.0f}s — robot should be breathing.")
    print( "      Head: gentle ~5 mm z-axis bob (~6 breaths/min).")
    print( "      Antennas: slow ~15° sway, 0.5 Hz.\n")

    # Send NO audio. Just wait.
    deadline = time.perf_counter() + WATCH_SECONDS
    while time.perf_counter() < deadline:
        # Progress dots, one per second
        time.sleep(1.0)
        elapsed = time.perf_counter() - t0
        print(f"  t={elapsed:5.1f}s", flush=True)

    print("\nsending EOF to robot…")
    channel.shutdown_write()
    rc = channel.recv_exit_status()
    stop.set()
    drainer.join(timeout=2.0)
    print(f"robot exited rc={rc}")
    c.close()
    print("\n--- full robot stderr log ---")
    for s in lines:
        print(f"  {s}")
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
