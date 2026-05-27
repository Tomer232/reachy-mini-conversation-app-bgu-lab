"""Probe robot SDK for the motion APIs Phase 3A needs.

Verifies that the following symbols exist and prints their signatures:
  - ReachyMini.set_target (head=, antennas=, body_yaw=)
  - ReachyMini.read_present_position / get_current_state-equivalent (to seed BreathingMove)
  - reachy_mini.utils.create_head_pose
  - reachy_mini.utils.interpolation.compose_world_offset
  - reachy_mini.utils.interpolation.linear_pose_interpolation
"""
import paramiko, textwrap

probe = textwrap.dedent('''
import inspect
import numpy as np

from reachy_mini import ReachyMini
try:
    from reachy_mini.utils import create_head_pose
    print("create_head_pose:", inspect.signature(create_head_pose))
except Exception as e:
    print("create_head_pose: MISSING", e)

try:
    from reachy_mini.utils.interpolation import compose_world_offset, linear_pose_interpolation
    print("compose_world_offset:", inspect.signature(compose_world_offset))
    print("linear_pose_interpolation:", inspect.signature(linear_pose_interpolation))
except Exception as e:
    print("interpolation helpers MISSING:", e)

try:
    from reachy_mini.motion.move import Move
    print("Move base class:", Move, "abstract methods:",
          getattr(Move, "__abstractmethods__", "n/a"))
except Exception as e:
    print("Move base class MISSING:", e)

with ReachyMini() as mini:
    print("\\n--- ReachyMini methods (non-_) ---")
    for n in sorted(dir(mini)):
        if n.startswith("_"):
            continue
        x = getattr(mini, n)
        if callable(x):
            try:
                sig = inspect.signature(x)
            except (TypeError, ValueError):
                sig = "(?)"
            print(f"  {n}{sig}")
    print("\\n--- set_target signature ---")
    try:
        print(inspect.signature(mini.set_target))
    except Exception as e:
        print("ERR:", e)

    # Try to read a starting pose to seed BreathingMove
    print("\\n--- read present state attempts ---")
    for name in ("read_present_position", "get_present_position", "get_target",
                 "get_current_state", "get_state", "present_head_pose"):
        fn = getattr(mini, name, None)
        if fn is not None:
            try:
                r = fn() if callable(fn) else fn
                print(f"  {name} -> type={type(r).__name__}, value preview={r if not hasattr(r, 'shape') else f'shape={r.shape}'}")
            except Exception as e:
                print(f"  {name} -> raised {type(e).__name__}: {e}")
        else:
            print(f"  {name}: not found")
''')

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.100.102.18", username="pollen", password="root", timeout=15)
sftp = c.open_sftp()
with sftp.open("/tmp/_probe_motion.py", "w") as f:
    f.write(probe)
sftp.close()

_, o, e = c.exec_command("/venvs/mini_daemon/bin/python /tmp/_probe_motion.py")
print(o.read().decode("utf-8", errors="replace"))
err = e.read().decode("utf-8", errors="replace")
if err:
    print("--- STDERR ---")
    print(err)
c.close()
