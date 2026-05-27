"""SFTP robot_streaming_player.py to /home/pollen/scripts/ and verify."""
import paramiko
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCAL = ROOT / "robot_streaming_player.py"
REMOTE = "/home/pollen/scripts/robot_streaming_player.py"

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.100.102.18", username="pollen", password="root", timeout=15)
sftp = c.open_sftp()
sftp.put(str(LOCAL), REMOTE)
sftp.chmod(REMOTE, 0o755)
sftp.close()

# Verify it landed and is syntactically valid Python
_, o, e = c.exec_command(
    f"ls -la {REMOTE} && /venvs/mini_daemon/bin/python -c "
    f"\"import py_compile; py_compile.compile('{REMOTE}', doraise=True); print('compile ok')\""
)
print(o.read().decode().rstrip())
err = e.read().decode().rstrip()
if err:
    print("STDERR:", err)
c.close()
