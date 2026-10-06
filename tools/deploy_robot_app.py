#!/usr/bin/env python3
"""Push the whole app to the robot so it can run there in robot mode.

`deploy_robot_player.py` ships one file — the audio/motion player that both
modes drive. This ships the *app*: the turn loop, the dashboard, the ONNX VAD
and its weights, so `laptop_chat.py --local-robot` can run on the robot itself
with the K11 receiver in the robot's own USB.

    python tools\\deploy_robot_app.py                 # sync + verify
    python tools\\deploy_robot_app.py --with-show     # also push show/ audio
    python tools\\deploy_robot_app.py --check         # verify only, no copy

Lands in /home/pollen/reachy_chat/. Only files whose size or mtime differ are
uploaded, so a re-deploy after one edit is quick. The robot's copy of
`~/scripts/robot_streaming_player.py` is left alone — that is
deploy_robot_player.py's job, and both modes share it.

Verification (always runs, even with --check) covers the things that actually
broke during the port: every module has to *import* under the robot's venv,
which is where a stray `import torch` or `import paramiko` would surface.
"""

from __future__ import annotations

import argparse
import stat
import sys
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fleet  # noqa: E402
from conversation import (ROBOT_HOST, ROBOT_USER, ROBOT_PASSWORD,  # noqa: E402
                          ROBOT_PYTHON)

REMOTE_ROOT = "/home/pollen/reachy_chat"

# Everything the app needs to start. Deliberately explicit rather than a glob:
# a deploy that silently starts shipping .venv/ or conversations/ would fill
# the SD card, which has ~3.7 GB free.
FILES = [
    "laptop_chat.py",
    "conversation.py",
    "system.py",
    # The fleet modules: who this robot is, whose key it talks through, and
    # which character it is wearing. Without these the app will not import.
    "identity.py",
    "credentials.py",
    "persona.py",
    "fleet.py",
    "providers/__init__.py",
    "providers/base.py",
    "providers/gemini.py",
    "providers/gpt_live.py",
    "providers/elevenlabs_voice.py",
    # The dashboard's backend picker (brain / language / ElevenLabs voice).
    "backend.py",
    # Gestures for gpt-live-1, which has no motion tools of its own.
    "motion_director.py",
    # Camera vision: frames from the daemon's camera socket to the brain.
    "vision.py",
    "vad.py",
    "vad_onnx.py",
    "local_transport.py",
    "event_log.py",
    "logging_setup.py",
    "show_player.py",
    # Editing the show script from the board. build_show is not optional here:
    # show_editor imports it for synthesise()/text_hash() so a line edited in
    # the browser is byte-identical to one built from the terminal.
    "show_editor.py",
    "tools/build_show.py",
    "summary.py",
    "robot_play.py",
    "models/silero_vad.onnx",
    "web/__init__.py",
    "web/broadcaster.py",
    "web/server.py",
]

# Directories copied wholesale (recursively), skipping __pycache__.
DIRS = ["web/static"]

SHOW_DIRS = ["show"]

# Imported one at a time under the robot's interpreter. An ImportError here is
# the whole point of the check.
IMPORT_CHECKS = [
    "vad_onnx",
    "local_transport",
    "identity",
    "credentials",
    "persona",
    "providers",
    "vision",
    "conversation",
    "show_editor",
    "system",
    "web.server",
]

# Optional, per-robot, and never overwritten if already on the robot:
# presets.json is the lab's curated persona list and someone may have edited
# it there; persona.json is *this robot's own* saved character and clobbering
# it would silently re-base a robot somebody configured this morning.
# keys.json is deliberately absent: keys live in robot-hub only.
OPTIONAL_FILES = ["presets.json", "robots.json"]
NEVER_OVERWRITE = {"presets.json", "persona.json", "robot_identity.json"}

# Libraries the app needs that the daemon venv does not ship. The 2026-07-27
# unit had them; factory robots on daemon 1.11.0 do not (reachy2, 2026-09-23).
# Measured with `pip install --dry-run`: these only *add* packages, nothing the
# daemon already uses is upgraded, so installing into its venv is safe.
PIP_DEPS = {"google.genai": "google-genai", "sounddevice": "sounddevice",
            "soundfile": "soundfile"}


def _ensure_deps(c: paramiko.SSHClient) -> None:
    missing = []
    for module, package in PIP_DEPS.items():
        _in, out, _err = c.exec_command(f"{ROBOT_PYTHON} -c 'import {module}'")
        if out.channel.recv_exit_status() != 0:
            missing.append(package)
    if not missing:
        print("  python deps: already installed")
        return
    pip = ROBOT_PYTHON.rsplit("/", 1)[0] + "/pip"
    print(f"  installing into the daemon venv: {' '.join(missing)} ...")
    _in, out, err = c.exec_command(f"{pip} install {' '.join(missing)}",
                                   timeout=600)
    text = (out.read() + err.read()).decode("utf-8", "replace").strip()
    if out.channel.recv_exit_status() != 0:
        raise RuntimeError("pip install failed: " + text[-400:])
    print("  " + (text.splitlines()[-1] if text else "installed"))


def _iter_dir(local_dir: Path):
    for p in sorted(local_dir.rglob("*")):
        if p.is_dir() or "__pycache__" in p.parts or p.suffix == ".pyc":
            continue
        yield p


def _mkdirs(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    parts, cur = remote_dir.strip("/").split("/"), ""
    for part in parts:
        cur += "/" + part
        try:
            sftp.stat(cur)
        except IOError:
            sftp.mkdir(cur)


def _needs_upload(sftp: paramiko.SFTPClient, local: Path, remote: str) -> bool:
    try:
        st = sftp.stat(remote)
    except IOError:
        return True
    lst = local.stat()
    if st.st_size != lst.st_size:
        return True
    # 2 s slack: SFTP mtimes are whole seconds and clocks are not in lockstep.
    return (st.st_mtime or 0) + 2 < lst.st_mtime


def deploy_one(robot, args) -> int:
    print(f"\n=== {robot.label} ({robot.host}) ===")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(robot.host, username=ROBOT_USER,
              password=ROBOT_PASSWORD, timeout=15)

    if not args.check:
        sftp = c.open_sftp()
        _mkdirs(sftp, REMOTE_ROOT)

        planned: list[tuple[Path, str]] = []
        for rel in FILES:
            local = ROOT / rel
            if not local.is_file():
                print(f"  MISSING locally, skipped: {rel}")
                continue
            planned.append((local, f"{REMOTE_ROOT}/{rel}"))

        dirs = list(DIRS) + (SHOW_DIRS if args.with_show else [])
        for d in dirs:
            local_dir = ROOT / d
            if not local_dir.is_dir():
                print(f"  MISSING locally, skipped: {d}/")
                continue
            for p in _iter_dir(local_dir):
                rel = p.relative_to(ROOT).as_posix()
                planned.append((p, f"{REMOTE_ROOT}/{rel}"))

        sent = skipped = 0
        total_bytes = 0
        for local, remote in planned:
            if not _needs_upload(sftp, local, remote):
                skipped += 1
                continue
            _mkdirs(sftp, remote.rsplit("/", 1)[0])
            sftp.put(str(local), remote)
            sftp.utime(remote, (local.stat().st_atime, local.stat().st_mtime))
            if remote.endswith(".py"):
                sftp.chmod(remote, 0o755)
            sent += 1
            total_bytes += local.stat().st_size
            print(f"  -> {local.relative_to(ROOT).as_posix()}")

        # Per-robot config that may already exist on the robot and must not be
        # trampled. A robot configured this morning keeps its persona.
        for rel in OPTIONAL_FILES:
            local = ROOT / rel
            if not local.is_file():
                continue
            remote = f"{REMOTE_ROOT}/{rel}"
            if rel in NEVER_OVERWRITE:
                try:
                    sftp.stat(remote)
                    print(f"  kept the robot's own {rel}")
                    continue
                except IOError:
                    pass
            if _needs_upload(sftp, local, remote):
                sftp.put(str(local), remote)
                if rel == "keys.json":
                    sftp.chmod(remote, stat.S_IRUSR | stat.S_IWUSR)   # 0600
                print(f"  -> {rel}")
                sent += 1

        # No key is ever stored on a robot any more: robot-hub owns the keys
        # and pipes the assigned one into the process at launch (Tomer,
        # 2026-09-24). Remove the copy earlier deploys left behind.
        try:
            sftp.remove(f"{REMOTE_ROOT}/.gemini_key")
            print("  removed the old .gemini_key (keys now come from the hub)")
        except IOError:
            pass

        sftp.close()
        print(f"\n{sent} file(s) uploaded, {skipped} unchanged, "
              f"{total_bytes/1024:.0f} KB")

        # A factory-fresh robot has neither the app's libraries nor the
        # player; one command should leave it ready to launch.
        _ensure_deps(c)
        import deploy_robot_player  # sibling tool; tools/ is sys.path[0]
        if not deploy_robot_player.deploy_one(robot):
            raise RuntimeError("the robot player did not deploy")

    # --- verify ---------------------------------------------------------
    print("\nverifying on the robot:")
    checks = " && ".join(
        f"{ROBOT_PYTHON} -c 'import {m}' && echo '  import {m}: ok'"
        for m in IMPORT_CHECKS
    )
    cmd = (f"cd {REMOTE_ROOT} && ls models/silero_vad.onnx >/dev/null && "
           f"echo '  weights present: ok' && {checks}")
    _in, out, err = c.exec_command(cmd, timeout=180)
    stdout = out.read().decode("utf-8", "replace")
    stderr = err.read().decode("utf-8", "replace")
    rc = out.channel.recv_exit_status()
    c.close()

    print(stdout.rstrip())
    if rc != 0:
        print("\nFAILED:")
        print(stderr.rstrip())
        return 1
    print("\nDeploy OK. Start this robot with:")
    print(f"  ssh {ROBOT_USER}@{robot.host}")
    identity = ""
    if robot.id or robot.name:
        identity = (f" --robot-id {robot.id or 'unknown'}"
                    f" --robot-name {robot.name or robot.id}")
    print(f"  cd {REMOTE_ROOT} && {ROBOT_PYTHON} laptop_chat.py "
          f"--local-robot --host 0.0.0.0 --no-browser{identity} "
          f"--mic-match reachymini_audio_src --mic-rate 16000")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--robot-host", default=None,
                    help="one or more robots, comma separated; a roster name, "
                         "an id, or a plain address")
    ap.add_argument("--all", action="store_true",
                    help="every robot in robots.json")
    ap.add_argument("--with-show", action="store_true",
                    help="also push show/ (cues + generated WAVs) so show mode "
                         "works from the robot")
    ap.add_argument("--check", action="store_true",
                    help="verify the existing deploy; copy nothing")
    args = ap.parse_args()

    targets = fleet.resolve_targets(args.robot_host, args.all,
                                    default_host=ROBOT_HOST)
    results = []
    for robot in targets:
        try:
            rc = deploy_one(robot, args)
        except Exception as exc:  # noqa: BLE001
            # One unreachable robot must not abandon the other nine.
            print(f"  FAILED: {exc}")
            rc = 1
        results.append((robot, rc))

    failed = [r for r, rc in results if rc != 0]
    print(f"\n{len(results) - len(failed)} of {len(results)} robot(s) deployed")
    if failed:
        print("failed: " + ", ".join(f"{r.label} ({r.host})" for r in failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
