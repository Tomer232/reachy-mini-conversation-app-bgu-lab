"""SFTP a probe script onto the robot, run it, capture output."""
import paramiko, textwrap

probe = textwrap.dedent('''
import inspect
from reachy_mini import ReachyMini

def dump(obj, label):
    print(f"--- public methods/attrs on {label} ---")
    for n in sorted(dir(obj)):
        if n.startswith("_"):
            continue
        try:
            x = getattr(obj, n)
        except Exception as e:
            print(f"  {n}: <get-failed: {e}>")
            continue
        if callable(x):
            try:
                sig = inspect.signature(x)
            except (ValueError, TypeError):
                sig = "(?)"
            print(f"  {n}{sig}")
        else:
            print(f"  {n} = {x!r}")

with ReachyMini() as mini:
    media = mini.media
    print("media type:", type(media).__name__)
    dump(media, "mini.media")
    audio = getattr(media, "audio", None)
    if audio is not None:
        print("audio type:", type(audio).__name__)
        dump(audio, "mini.media.audio")
    else:
        print("no mini.media.audio attribute")

    # If push/start/stop methods are missing on .media, search the SDK module
    import reachy_mini
    print("reachy_mini module file:", getattr(reachy_mini, "__file__", "?"))
''')

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.100.102.18", username="pollen", password="root", timeout=15)
sftp = c.open_sftp()
with sftp.open("/tmp/_probe.py", "w") as f:
    f.write(probe)
sftp.close()

_, o, e = c.exec_command("/venvs/mini_daemon/bin/python /tmp/_probe.py")
out = o.read().decode("utf-8", errors="replace")
err = e.read().decode("utf-8", errors="replace")
print(out)
if err:
    print("--- STDERR ---")
    print(err)
c.close()
