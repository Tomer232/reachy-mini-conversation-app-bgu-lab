import paramiko
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.100.102.18", username="pollen", password="root", timeout=10)
for cmd in [
    "ls -la /home/pollen/scripts/robot_play.py",
    "/venvs/mini_daemon/bin/python -c 'import reachy_mini, soundfile, numpy, scipy; print(\"robot venv OK\")'",
    "hostname",
]:
    _, o, e = c.exec_command(cmd)
    print(f"$ {cmd}")
    print(o.read().decode().rstrip())
    err = e.read().decode().rstrip()
    if err:
        print("  STDERR:", err)
c.close()
