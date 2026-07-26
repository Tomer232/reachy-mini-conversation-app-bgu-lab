"""Pre-show check: mic, robot, API key, cue audio. Run before laptop_chat.py.

Answers the four questions that account for nearly every failed start:

  1. Is the lavalier receiver actually feeding audio? (device present is not
     the same as transmitter on — a receiver that enumerates fine can deliver
     digital silence.)
  2. Is the robot reachable on this network, and is its daemon alive?
  3. Does a Gemini API key resolve?
  4. Does every show cue have audio matching its current text? (An edited
     script with stale audio means Reachy says the old line on stage.)

Prints the exact command to run at the end. Exit code 0 if everything passed,
1 otherwise.

    .venv\\Scripts\\python.exe tools\\preflight.py
    .venv\\Scripts\\python.exe tools\\preflight.py --robot-host 10.100.102.99
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import sounddevice as sd

import conversation as conv

SSH_PORT = 22
DAEMON_PORT = 8000
PROBE_TIMEOUT_S = 2.0
MIC_SECONDS = 2.0
# Quiet-room speech sits near -35 dBFS on this receiver; digital silence reads
# about -96. Anything below this is "the transmitter is not sending".
MIC_SILENCE_DBFS = -70.0


def _dbfs(v: float) -> float:
    return 20 * np.log10(v / 32768.0) if v > 0 else float("-inf")


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=PROBE_TIMEOUT_S):
            return True
    except Exception:
        return False


def check_mic() -> bool:
    print("[1/4] Microphone")
    try:
        info = conv._ensure_input_device()
    except Exception as e:
        print(f"      FAIL  could not enumerate audio devices: {e}")
        return False
    print(f"      device: {info['name']} (host={info['host_api']})")
    if info["device"] is None and conv.INPUT_DEVICE:
        # Falling back to the built-in mic is the failure this check exists to
        # catch: it still produces sound, so a conversation "works" while the
        # robot hears the room instead of the speaker.
        print(f"      FAIL  no input device matches '{conv.INPUT_DEVICE}'. The lavalier")
        print("            receiver is unplugged or powered off — plug the USB receiver")
        print("            back in and re-run. (Falling back to the laptop's built-in")
        print("            mic would pick up the room and Reachy's own voice.)")
        return False

    try:
        audio = sd.rec(int(conv.GEMINI_INPUT_RATE * MIC_SECONDS),
                       samplerate=conv.GEMINI_INPUT_RATE, channels=1,
                       dtype="int16", device=info["device"])
        sd.wait()
    except Exception as e:
        print(f"      FAIL  could not open the input stream: {e}")
        return False

    audio = audio[:, 0] if audio.ndim > 1 else audio
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2))) if audio.size else 0.0
    peak = int(np.max(np.abs(audio))) if audio.size else 0
    print(f"      level : rms {_dbfs(rms):+.1f} dBFS, peak {_dbfs(peak):+.1f} dBFS "
          f"({MIC_SECONDS:.0f}s sample)")
    if _dbfs(rms) < MIC_SILENCE_DBFS:
        print("      FAIL  silence. Switch the lavalier transmitter on, check it is")
        print("            paired with the receiver and not muted, then re-run.")
        return False
    print("      OK    signal present")
    return True


def check_robot(cli_host: str | None) -> tuple[bool, str]:
    print("[2/4] Robot")
    host, source = conv.get_robot_host(cli_host)
    print(f"      host  : {host} (from {source})")
    try:
        ip = socket.gethostbyname(host)
        if ip != host:
            print(f"      resolves to {ip}")
    except Exception:
        print(f"      FAIL  {host} does not resolve.")
        return False, host

    ssh_ok = _port_open(host, SSH_PORT)
    daemon_ok = _port_open(host, DAEMON_PORT)
    print(f"      ssh   : {'open' if ssh_ok else 'CLOSED'} (port {SSH_PORT})")
    print(f"      daemon: {'open' if daemon_ok else 'CLOSED'} (port {DAEMON_PORT})")
    if ssh_ok and daemon_ok:
        print("      OK    robot is on this network")
        return True, host

    print("      FAIL  cannot reach the robot. Check that it is powered on and")
    print("            joined to the same WiFi as this laptop, then find its IP")
    print("            on the robot's screen or in your router's client list and")
    print("            pass it with --robot-host.")
    return False, host


def check_show() -> bool:
    """Every cue with text must have audio whose hash matches the current line.
    Catches the classic failure: the script was edited, build_show.py was not
    re-run, and the robot says the old line on stage."""
    print("[4/4] Show cues")
    cues_path = ROOT / "show" / "cues.json"
    if not cues_path.exists():
        print("      skip  no show/cues.json (show mode not in use)")
        return True

    try:
        sys.path.insert(0, str(ROOT / "tools"))
        import build_show
        data = build_show.load_cues()
    except SystemExit as e:
        print(f"      FAIL  cue file invalid: {e}")
        return False
    except Exception as e:
        print(f"      FAIL  could not read the cue file: {e}")
        return False

    problems = build_show.validate_motions(data)
    manifest = {}
    manifest_path = ROOT / "show" / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8")).get("cues", {})
        except Exception:
            problems.append("manifest.json unreadable")

    voice = data.get("voice", "")
    style = data.get("style", "")
    spoken = motion_only = 0
    for _section, cue in build_show.iter_cues(data):
        text = cue.get("text")
        if not text:
            motion_only += 1
            continue
        spoken += 1
        cid = cue["id"]
        wav = ROOT / "show" / "audio" / f"{cid}.wav"
        entry = manifest.get(cid)
        if not wav.exists():
            problems.append(f"{cid}: audio missing")
        elif not entry:
            problems.append(f"{cid}: not in manifest")
        elif entry.get("hash") != build_show.text_hash(text, voice, style):
            problems.append(f"{cid}: text changed since the audio was generated")

    print(f"      cues  : {spoken} spoken, {motion_only} motion-only")
    if problems:
        for p in problems[:10]:
            print(f"      FAIL  {p}")
        print("            run: .\\.venv\\Scripts\\python.exe tools\\build_show.py")
        return False
    print("      OK    all cue audio present and current")
    return True


def check_api_key() -> bool:
    print("[3/4] Gemini API key")
    try:
        key = conv.get_api_key()
    except SystemExit as e:
        print(f"      FAIL  {e}")
        return False
    print(f"      OK    key resolved ({len(key)} chars)")
    return True


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Pre-conversation checks.")
    ap.add_argument("--robot-host", default=None,
                    help="Robot address to test instead of the configured one.")
    args = ap.parse_args()

    mic_ok = check_mic()
    print()
    robot_ok, host = check_robot(args.robot_host)
    print()
    key_ok = check_api_key()
    print()
    show_ok = check_show()
    print()

    if mic_ok and robot_ok and key_ok and show_ok:
        print("All checks passed. Start the dashboard with:")
        print(f"    .\\.venv\\Scripts\\python.exe laptop_chat.py --robot-host {host}")
        print("Conversation: http://127.0.0.1:8765/    Operator board: /show")
        return 0
    print("Fix the FAIL lines above, then run this again.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
