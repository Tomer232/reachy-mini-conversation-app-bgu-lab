#!/usr/bin/env python3
"""SFTP robot_streaming_player.py to /home/pollen/scripts/ and verify.

One file -- the audio/motion player both modes drive. `deploy_robot_app.py`
ships the app; this ships the player, and the two are deliberately separate
because the player changes far less often.

    python tools\\deploy_robot_player.py                      # the default host
    python tools\\deploy_robot_player.py --robot-host Rina    # one robot
    python tools\\deploy_robot_player.py --all                # the whole roster

The address used to be a literal in this file, which was honest when there was
one robot and is a trap with ten: a deploy that silently goes to the wrong body
looks exactly like a deploy that did nothing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fleet  # noqa: E402
from conversation import (ROBOT_HOST, ROBOT_USER, ROBOT_PASSWORD,  # noqa: E402
                          ROBOT_PYTHON)

LOCAL = ROOT / "robot_streaming_player.py"
REMOTE = "/home/pollen/scripts/robot_streaming_player.py"
TAPPER_REMOTE = "/home/pollen/scripts/robot_speech_tapper.py"


def deploy_one(robot) -> bool:
    """Returns True when the player landed and compiled on that robot."""
    print("\n=== {} ({}) ===".format(robot.label, robot.host))
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(robot.host, username=ROBOT_USER, password=ROBOT_PASSWORD,
                  timeout=15)
    except Exception as exc:  # noqa: BLE001
        print("  could not connect: {}".format(exc))
        return False

    try:
        # A factory-fresh robot has no scripts/ folder yet (reachy2, 2026-09-23).
        c.exec_command("mkdir -p {}".format(REMOTE.rsplit("/", 1)[0]))[1].read()
        sftp = c.open_sftp()
        sftp.put(str(LOCAL), REMOTE)
        sftp.chmod(REMOTE, 0o755)
        # The player imports this from its own folder; a robot that only ever
        # got the player fails with ModuleNotFoundError before saying 'ready'.
        sftp.put(str(ROOT / "robot_speech_tapper.py"), TAPPER_REMOTE)
        sftp.close()

        # Verify it landed and is syntactically valid Python under the robot's
        # own interpreter -- a file that arrives but does not compile is the
        # failure this check exists for.
        _, o, e = c.exec_command(
            "ls -la {remote} && {py} -c \"import py_compile; "
            "py_compile.compile('{remote}', doraise=True); print('compile ok')\""
            .format(remote=REMOTE, py=ROBOT_PYTHON))
        out = o.read().decode("utf-8", "replace").rstrip()
        err = e.read().decode("utf-8", "replace").rstrip()
        rc = o.channel.recv_exit_status()
        for line in out.splitlines():
            print("  " + line)
        if rc != 0 or err:
            print("  STDERR: " + err)
            return False
        return True
    finally:
        c.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--robot-host", default=None,
                    help="one or more robots, comma separated; a roster name, "
                         "an id, or a plain address")
    ap.add_argument("--all", action="store_true",
                    help="every robot in robots.json")
    args = ap.parse_args()

    if not LOCAL.is_file():
        print("{} is not here -- nothing to deploy".format(LOCAL.name))
        return 1

    targets = fleet.resolve_targets(args.robot_host, args.all,
                                    default_host=ROBOT_HOST)
    results = [(r, deploy_one(r)) for r in targets]

    failed = [r for r, ok in results if not ok]
    print("\n{} of {} robot(s) updated".format(
        len(results) - len(failed), len(results)))
    if failed:
        # Named, not counted. "1 failed" out of ten is a number nobody can act
        # on at the moment they read it.
        print("failed: " + ", ".join("{} ({})".format(r.label, r.host)
                                     for r in failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
