"""Phase 3B — direct motion-command test, no Gemini, no microphone.

SSH-execs the robot streaming player, waits for 'ready', sends a few
motion commands directly via the new 0xFFFFFFFC sentinel + JSON payload,
sleeps between each so Tomer (at the robot) can see each one happen,
then EOF and clean exit.

Sequence:
  1. play_emotion(amazed1)
  2. dance(yeah_nod)
  3. move_head(left), move_head(right), move_head(up), move_head(down), move_head(front)
  4. play_emotion(curious1)
  5. stop  (back to breathing)
  6. EOF

Audio path is unused. Motion path is exercised end-to-end (laptop → SSH →
0xFFFFFFFC → robot dispatch → set_target).
"""
import sys
import time
import json
import struct
import threading

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

READY_TIMEOUT_S = 60.0  # robot side imports HF/dances libs at startup → slower
HEADER = struct.Struct(">I")
MOTION_SENTINEL = HEADER.pack(0xFFFFFFFC)


def frame_motion(cmd: dict) -> bytes:
    payload = json.dumps(cmd, ensure_ascii=False).encode("utf-8")
    return MOTION_SENTINEL + HEADER.pack(len(payload)) + payload


def stderr_drainer(channel, ready, lines, stop):
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
                    ready.set()
        elif channel.exit_status_ready():
            tail = channel.recv_stderr(65536)
            if tail:
                buf += tail
                continue
            break
        else:
            time.sleep(0.02)


def main() -> int:
    print(f"Connecting to {ROBOT_HOST} …")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(ROBOT_HOST, username=ROBOT_USER, password=ROBOT_PASSWORD, timeout=15)
    transport = c.get_transport()
    channel = transport.open_session()
    channel.exec_command(f"{ROBOT_PYTHON} -u {ROBOT_SCRIPT}")
    print("exec issued; waiting for 'ready' …")

    ready = threading.Event()
    lines: list[str] = []
    stop = threading.Event()
    drainer = threading.Thread(
        target=stderr_drainer, args=(channel, ready, lines, stop), daemon=True
    )
    drainer.start()

    t0 = time.perf_counter()
    if not ready.wait(timeout=READY_TIMEOUT_S):
        print(f"ERROR: robot never said 'ready' in {READY_TIMEOUT_S}s")
        channel.close()
        c.close()
        return 1
    print(f"\nROBOT READY in {time.perf_counter() - t0:.2f}s — running motion sequence\n")

    def send(cmd: dict, wait_s: float, label: str) -> None:
        print(f">>> {label}  cmd={cmd}")
        channel.send(frame_motion(cmd))
        print(f"    (sleeping {wait_s:.1f}s)")
        time.sleep(wait_s)

    # Sequence (durations: emotions ~2–4s, dances ~6–10s, head ~1s)
    send({"type": "emotion", "name": "amazed1"},   4.0, "amazed1")
    send({"type": "dance",   "name": "yeah_nod"}, 10.0, "yeah_nod dance")
    send({"type": "head",    "direction": "left"}, 1.5, "look left")
    send({"type": "head",    "direction": "right"},1.5, "look right")
    send({"type": "head",    "direction": "up"},   1.5, "look up")
    send({"type": "head",    "direction": "down"}, 1.5, "look down")
    send({"type": "head",    "direction": "front"},1.5, "look front")
    send({"type": "emotion", "name": "curious1"},  4.0, "curious1")
    send({"type": "stop"},                          3.0, "stop → breathing")

    print("\nsending EOF …")
    channel.shutdown_write()
    rc = channel.recv_exit_status()
    stop.set()
    drainer.join(timeout=2.0)
    print(f"robot exited rc={rc}")
    c.close()
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
