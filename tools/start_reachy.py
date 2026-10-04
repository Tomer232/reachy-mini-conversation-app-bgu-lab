#!/usr/bin/env python3
"""One entry point for talking to Reachy: find the robot, find the mic, start
the right app.

Run it from the reachy-mini folder (``.\\start.ps1``) and it will:

  1. Scan the current network for the robot, so its address never has to be
     looked up or pasted by hand again.
  2. Look for the K11 receiver on *both* machines and check each is actually
     delivering signal — a receiver that enumerates while its transmitter is
     off reads as present but sends digital silence, which is invisible from
     the outside and has cost real debugging time.
  3. Open a page on this laptop with what it found and the matching mode
     preselected. You confirm (or override) and press Start.
  4. Stop anything already running, start the chosen mode, and open the
     dashboard.

The two modes:

  Robot mode   K11 in the robot's USB. The app runs on the robot; the
               dashboard is served from the robot and opened here. The laptop
               can be anywhere on the network — off-stage, in your bag.
  Laptop mode  K11 in this laptop's USB. The original arrangement: the laptop
               listens and streams audio to the robot over SSH.

Nothing here is required. Both modes are still startable by hand, and this
script only ever runs the same commands you would type.
"""

from __future__ import annotations

import argparse
import http.server
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from find_robot import local_ipv4, port_open, identify  # noqa: E402
import concurrent.futures  # noqa: E402

LAUNCHER_PORT = 8770
DASHBOARD_PORT = 8765
ROBOT_USER = "pollen"
ROBOT_PASSWORD = "root"
ROBOT_PYTHON = "/venvs/mini_daemon/bin/python"
ROBOT_APP_DIR = "/home/pollen/reachy_chat"
ROBOT_LOG = "/tmp/reachy_app.log"
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

# How long to wait for a dashboard to start accepting connections. The robot
# pays SDK init (~7 s) plus VAD load plus the Gemini client, and does it on a
# Pi rather than the laptop.
DASHBOARD_WAIT_S = 120.0

# A receiver whose transmitter is off returns *exactly* zero — not a low level,
# digital silence. Any real linked receiver shows at least room noise, which
# measured ~2e-4 RMS on the robot. So a peak above this is "the link is up",
# and it is a far more reliable question than "is it loud enough".
SIGNAL_FLOOR = 1e-5
PROBE_SECONDS = 1.0


# ===== state ========================================================

class State:
    """Everything the page shows, plus whatever we started."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.scanning = False
        self.scanned_at = 0.0
        self.laptop_ip: str | None = None
        self.robot: dict | None = None       # {ip, name, state, version}
        self.laptop_mic: dict = {"status": "unknown"}
        self.robot_mic: dict = {"status": "unknown"}
        self.running: str | None = None      # "robot" | "laptop" | None
        self.dashboard_url: str | None = None
        self.child: subprocess.Popen | None = None
        self.message = ""
        self.busy = False

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "scanning": self.scanning,
                "busy": self.busy,
                "laptop_ip": self.laptop_ip,
                "robot": self.robot,
                "laptop_mic": self.laptop_mic,
                "robot_mic": self.robot_mic,
                "running": self.running,
                "dashboard_url": self.dashboard_url,
                "message": self.message,
                "recommendation": self._recommend(),
            }

    def _recommend(self) -> dict:
        """Which mode the evidence points at, and why — in words that say what
        was actually observed rather than just naming a winner."""
        lm, rm = self.laptop_mic, self.robot_mic
        if self.robot is None:
            return {"mode": None,
                    "why": "No robot found on this network yet."}
        rl = rm.get("status") == "live"
        ll = lm.get("status") == "live"
        if rl and not ll:
            return {"mode": "robot",
                    "why": f"K11 is in the robot and sending signal "
                           f"({rm.get('peak_dbfs', 0):.0f} dBFS peak)."}
        if ll and not rl:
            return {"mode": "laptop",
                    "why": f"K11 is in this laptop and sending signal "
                           f"({lm.get('peak_dbfs', 0):.0f} dBFS peak)."}
        if rl and ll:
            return {"mode": "robot",
                    "why": "A live mic was found on both machines. Robot mode "
                           "is the guess; pick Laptop if that is the one you "
                           "are wearing."}
        # Nothing live. Say precisely what is wrong, because "no mic" and
        # "mic present but muted" need different fixes.
        for side, mic, name in (("robot", rm, "the robot"),
                                ("laptop", lm, "this laptop")):
            if mic.get("status") == "silent":
                return {"mode": side,
                        "why": f"A receiver is plugged into {name} but sending "
                               f"digital silence — the transmitter is off, "
                               f"muted, or unpaired. Switch it on and rescan."}
        return {"mode": None,
                "why": "No K11 receiver found on either machine. Plug it in "
                       "and rescan."}


STATE = State()


# ===== probing ======================================================

def _probe_snippet(needle: str, rate: int, seconds: float) -> str:
    """Python one-liner used on both sides so the two answers are comparable."""
    return (
        "import json,sys\n"
        "try:\n"
        "    import sounddevice as sd, numpy as np\n"
        f"    needle={needle!r}.lower()\n"
        "    devs=sd.query_devices()\n"
        "    idx=[i for i,d in enumerate(devs)\n"
        "         if d['max_input_channels']>0 and needle in d['name'].lower()]\n"
        "    if not idx:\n"
        "        print(json.dumps({'status':'absent'})); sys.exit()\n"
        "    i=idx[0]; name=devs[i]['name']\n"
        f"    a=sd.rec(int({seconds}*{rate}), samplerate={rate}, channels=1,\n"
        "             dtype='float32', device=i); sd.wait()\n"
        "    a=a[:,0]\n"
        "    rms=float(np.sqrt((a**2).mean())); peak=float(np.abs(a).max())\n"
        "    print(json.dumps({'status':'probed','name':name,\n"
        "                      'rms':rms,'peak':peak}))\n"
        "except Exception as e:\n"
        "    print(json.dumps({'status':'error','detail':str(e)}))\n"
    )


def _finish_probe(raw: dict) -> dict:
    """Turn a raw {rms, peak} reading into a status the page can show."""
    import math
    if raw.get("status") != "probed":
        return raw
    peak, rms = raw.get("peak", 0.0), raw.get("rms", 0.0)
    db = lambda x: 20 * math.log10(x) if x > 0 else -999.0   # noqa: E731
    raw["peak_dbfs"] = db(peak)
    raw["rms_dbfs"] = db(rms)
    raw["status"] = "live" if peak > SIGNAL_FLOOR else "silent"
    return raw


def probe_laptop_mic() -> dict:
    """Is the K11 in this laptop, and is it sending anything?"""
    code = _probe_snippet("USBAudio", 16000, PROBE_SECONDS)
    try:
        p = subprocess.run([str(VENV_PYTHON), "-c", code],
                           capture_output=True, text=True, timeout=30)
        line = next((ln for ln in p.stdout.splitlines() if ln.startswith("{")),
                    None)
        if line is None:
            return {"status": "error", "detail": (p.stderr or "no output")[-200:]}
        return _finish_probe(json.loads(line))
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def probe_robot_mic(ip: str) -> dict:
    """Same question, asked on the robot. 48 kHz because the robot's ALSA
    device offers nothing else."""
    code = _probe_snippet("Composite", 48000, PROBE_SECONDS)
    try:
        import paramiko
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(ip, username=ROBOT_USER, password=ROBOT_PASSWORD, timeout=10)
        # The heredoc avoids quoting the snippet through two shells.
        _i, out, _e = c.exec_command(
            f"{ROBOT_PYTHON} - <<'PYEOF'\n{code}\nPYEOF", timeout=60)
        stdout = out.read().decode("utf-8", "replace")
        c.close()
        line = next((ln for ln in stdout.splitlines() if ln.startswith("{")),
                    None)
        if line is None:
            return {"status": "error", "detail": stdout[-200:] or "no output"}
        return _finish_probe(json.loads(line))
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def find_robot() -> dict | None:
    """Scan this laptop's own /24 for the robot daemon."""
    me = local_ipv4()
    with STATE.lock:
        STATE.laptop_ip = me
    if me is None:
        return None
    subnet = me.rsplit(".", 1)[0]
    hits: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=128) as pool:
        for hit in pool.map(port_open,
                            [f"{subnet}.{i}" for i in range(1, 255)]):
            if hit:
                hits.append(hit)
    for ip in hits:
        info = identify(ip)
        if info:
            return {"ip": ip,
                    "name": info.get("robot_name", "reachy"),
                    "state": info.get("state"),
                    "version": info.get("version")}
    return None


def rescan() -> None:
    """Discovery + both mic probes. Runs on a worker thread."""
    with STATE.lock:
        if STATE.scanning:
            return
        STATE.scanning = True
        STATE.message = "Scanning the network for the robot…"
    try:
        robot = find_robot()
        with STATE.lock:
            STATE.robot = robot
            STATE.message = "Checking the microphone…"

        laptop_mic = probe_laptop_mic()
        robot_mic = ({"status": "no_robot"} if robot is None
                     else probe_robot_mic(robot["ip"]))
        with STATE.lock:
            STATE.laptop_mic = laptop_mic
            STATE.robot_mic = robot_mic
            STATE.scanned_at = time.time()
            STATE.message = ""
    except Exception as e:
        with STATE.lock:
            STATE.message = f"Scan failed: {e}"
    finally:
        with STATE.lock:
            STATE.scanning = False


# ===== starting and stopping ========================================

def _ssh(ip: str, cmd: str, timeout: float = 120.0) -> tuple[int, str, str]:
    import paramiko
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(ip, username=ROBOT_USER, password=ROBOT_PASSWORD, timeout=10)
    try:
        _i, out, err = c.exec_command(cmd, timeout=timeout)
        so = out.read().decode("utf-8", "replace")
        se = err.read().decode("utf-8", "replace")
        return out.channel.recv_exit_status(), so, se
    finally:
        c.close()


def _ssh_detached(ip: str, cmd: str) -> None:
    """Fire a command that keeps running after we hang up, without waiting.

    Reading stdout to EOF (what _ssh does) hangs here: the app we start
    outlives the SSH session by design, so the channel never reaches EOF and
    the read sits until it times out — reporting a failure for something that
    started perfectly well. Whether the app actually came up is answered by
    polling its port, not by this call.
    """
    import paramiko
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(ip, username=ROBOT_USER, password=ROBOT_PASSWORD, timeout=10)
    try:
        c.exec_command(cmd, timeout=10)
        time.sleep(1.0)   # let the remote shell get as far as the fork
    finally:
        c.close()


def port_listening(ip: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def open_both(base: str) -> None:
    """Open the conversation dashboard and the show board, in that order.

    Both boards are needed to run a lecture and /show is not a path anyone
    recalls under pressure, so neither should have to be typed. The gap is
    load-bearing: the first call may have to cold-start the browser, and a
    second open fired into a still-launching browser gets dropped. The show
    board goes last so it is the focused tab — it is the one being driven.
    """
    for i, url in enumerate((base, base.rstrip("/") + "/show")):
        if i:
            time.sleep(1.5)
        try:
            webbrowser.open(url)
        except Exception:
            print(f"Could not open {url} — open it by hand.")


def stop_everything(ip: str | None) -> list[str]:
    """Make sure exactly zero instances are running before we start one.

    Both modes drive the same robot through the same SDK and the same player
    process. Two of them at once fight over the audio device, and the symptom
    (robot moves but never speaks) looks exactly like the broken-mic problem
    this project already exists to work around. So this is not tidiness — it
    is the thing that keeps a demo from failing confusingly.
    """
    notes = []
    with STATE.lock:
        child = STATE.child
    if child is not None and child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
        notes.append("stopped the laptop app this launcher had started")
    with STATE.lock:
        STATE.child = None

    if ip:
        try:
            # The app on the robot, and any player left behind by either mode.
            #
            # The [l] / [r] bracket trick is load-bearing: pkill -f matches
            # against full command lines, and the shell running this very
            # command has "laptop_chat.py" in its own command line. Written
            # plainly, pkill would match and kill its own parent shell, so the
            # pgrep count after it would never run and we would report success
            # having verified nothing. As a regex "[l]aptop_chat" still matches
            # the real process; as a literal string it does not match itself.
            rc, so, _ = _ssh(ip, "pkill -f '[l]aptop_chat.py' ; "
                                 "pkill -f '[r]obot_streaming_player.py' ; "
                                 "sleep 1 ; "
                                 "pgrep -f '[l]aptop_chat.py|[r]obot_streaming_player.py' "
                                 "| wc -l", timeout=30)
            left = (so.strip().splitlines() or ["?"])[-1]
            notes.append(f"cleared robot-side processes (remaining: {left})")
        except Exception as e:
            notes.append(f"could not reach the robot to clear processes: {e}")

    if port_listening("127.0.0.1", DASHBOARD_PORT, timeout=0.5):
        notes.append(
            f"WARNING: something is still listening on 127.0.0.1:{DASHBOARD_PORT} "
            f"— a dashboard started by hand? Close that window first.")
    with STATE.lock:
        STATE.running = None
        STATE.dashboard_url = None
    return notes


def start_laptop_mode(ip: str) -> str:
    """The original arrangement, with the address filled in for you."""
    proc = subprocess.Popen(
        [str(VENV_PYTHON), "laptop_chat.py", "--robot-host", ip],
        cwd=str(ROOT),
    )
    with STATE.lock:
        STATE.child = proc
        STATE.running = "laptop"
        STATE.dashboard_url = f"http://127.0.0.1:{DASHBOARD_PORT}/"
    # laptop_chat.py opens its own browser; don't open a second tab.
    return f"Laptop mode starting (pid {proc.pid}). The dashboard will open."


def _key_exports() -> str:
    """`export NAME=key; ` for every provider key this laptop has, so the
    robot's dashboard can offer the same backends the laptop's would."""
    import shlex
    import credentials
    out = []
    for provider, env in ((credentials.GEMINI, "GEMINI_API_KEY"),
                          (credentials.GPT_LIVE, "OPENAI_API_KEY"),
                          (credentials.ELEVENLABS, "ELEVENLABS_API_KEY")):
        try:
            key = credentials.resolve(provider, use_hub=False).key
        except Exception:
            continue
        out.append("export {}={}; ".format(env, shlex.quote(key)))
    return "".join(out)


def start_robot_mode(ip: str) -> str:
    """Sync the code, start the app on the robot, wait for it to answer."""
    # Always sync first. It is incremental (unchanged files are skipped), and
    # the alternative is debugging a stale copy on the robot at the worst
    # possible moment.
    # --with-show: in robot mode the dashboard (and so the /show cue board) is
    # served from the robot, which means the cue WAVs have to be there too.
    # Without them show mode silently disables itself — a nasty surprise to
    # find mid-lecture. It is 4 MB and only re-copies what changed.
    dep = subprocess.run(
        [str(VENV_PYTHON), "tools/deploy_robot_app.py",
         "--robot-host", ip, "--with-show"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=600)
    if dep.returncode != 0:
        raise RuntimeError(
            f"deploy failed:\n{dep.stdout[-800:]}\n{dep.stderr[-400:]}")

    # setsid so the app outlives this SSH session. The API keys travel in the
    # app's environment, the way robot-hub hands them over: nothing is
    # written to the robot's disk (2026-09-24). Without this, robot mode
    # started from here had no key at all once .gemini_key was removed.
    cmd = (f"cd {ROBOT_APP_DIR} && {_key_exports()}"
           f"setsid nohup {ROBOT_PYTHON} laptop_chat.py --local-robot "
           f"--host 0.0.0.0 --no-browser > {ROBOT_LOG} 2>&1 < /dev/null & "
           f"echo started")
    _ssh_detached(ip, cmd)

    deadline = time.time() + DASHBOARD_WAIT_S
    while time.time() < deadline:
        if port_listening(ip, DASHBOARD_PORT, timeout=1.0):
            url = f"http://{ip}:{DASHBOARD_PORT}/"
            with STATE.lock:
                STATE.running = "robot"
                STATE.dashboard_url = url
            open_both(url)
            return f"Robot mode running. Dashboard: {url}"
        time.sleep(1.0)

    # Timed out — hand back the robot's own log, which will say why.
    _rc, tail, _ = _ssh(ip, f"tail -n 25 {ROBOT_LOG}", timeout=30)
    raise RuntimeError(
        f"the app on the robot never opened port {DASHBOARD_PORT} within "
        f"{DASHBOARD_WAIT_S:.0f}s. Last lines of {ROBOT_LOG}:\n{tail}")


def do_start(mode: str) -> None:
    """Worker: stop whatever is running, then start the requested mode."""
    with STATE.lock:
        if STATE.busy:
            return
        STATE.busy = True
        robot = STATE.robot
        STATE.message = "Stopping anything already running…"
    try:
        ip = robot["ip"] if robot else None
        notes = stop_everything(ip)
        if ip is None:
            raise RuntimeError("no robot on this network — nothing to start "
                               "against. Power it on and rescan.")
        with STATE.lock:
            STATE.message = ("Syncing code and starting on the robot…"
                             if mode == "robot" else "Starting on the laptop…")
        msg = (start_robot_mode(ip) if mode == "robot"
               else start_laptop_mode(ip))
        with STATE.lock:
            STATE.message = " ".join(notes + [msg])
    except Exception as e:
        with STATE.lock:
            STATE.running = None
            STATE.message = f"Failed: {e}"
    finally:
        with STATE.lock:
            STATE.busy = False


def do_stop() -> None:
    with STATE.lock:
        if STATE.busy:
            return
        STATE.busy = True
        robot = STATE.robot
    try:
        notes = stop_everything(robot["ip"] if robot else None)
        with STATE.lock:
            STATE.message = "Stopped. " + " ".join(notes)
    finally:
        with STATE.lock:
            STATE.busy = False


# ===== web ==========================================================

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Start Reachy</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin:0; font:16px/1.5 "Segoe UI",system-ui,sans-serif;
         background:#12141a; color:#e8eaf0;
         display:flex; justify-content:center; padding:32px 16px; }
  .wrap { width:100%; max-width:720px; }
  h1 { font-size:24px; margin:0 0 4px; font-weight:650; }
  .sub { color:#8b93a7; margin-bottom:24px; font-size:14px; }
  .card { background:#1a1d26; border:1px solid #262b38; border-radius:12px;
          padding:16px 18px; margin-bottom:14px; }
  .row { display:flex; justify-content:space-between; align-items:baseline;
         gap:12px; padding:5px 0; }
  .row + .row { border-top:1px solid #21252f; }
  .k { color:#8b93a7; font-size:14px; }
  .v { font-variant-numeric:tabular-nums; text-align:right; }
  .ok { color:#5ddc9a; } .bad { color:#ff8080; } .warn { color:#ffc861; }
  .rec { background:#16211c; border:1px solid #2b4739; border-radius:10px;
         padding:12px 14px; margin-bottom:18px; font-size:14px; }
  .rec.none { background:#231a1a; border-color:#4a2c2c; }
  .modes { display:grid; grid-template-columns:1fr 1fr; gap:12px;
           margin-bottom:14px; }
  button.mode { text-align:left; padding:16px; border-radius:12px; cursor:pointer;
       background:#1a1d26; color:#e8eaf0; border:1.5px solid #2c3141;
       font:inherit; transition:border-color .12s, background .12s; }
  button.mode:hover:not(:disabled) { border-color:#4a5268; }
  button.mode.pick { border-color:#43c98a; background:#16211c; }
  button.mode b { display:block; font-size:15px; margin-bottom:3px; }
  button.mode span { color:#8b93a7; font-size:13px; }
  button.mode:disabled { opacity:.5; cursor:not-allowed; }
  .actions { display:flex; gap:10px; }
  button.act { flex:0 0 auto; padding:10px 18px; border-radius:9px; cursor:pointer;
       font:inherit; border:1px solid #2c3141; background:#222736; color:#e8eaf0; }
  button.act:hover:not(:disabled) { background:#2a3042; }
  button.act:disabled { opacity:.5; cursor:not-allowed; }
  .msg { margin-top:16px; padding:12px 14px; border-radius:9px;
         background:#1a1d26; border:1px solid #262b38; font-size:14px;
         white-space:pre-wrap; display:none; }
  .msg.show { display:block; }
  a { color:#6fb4ff; }
  .spin { display:inline-block; width:11px; height:11px; margin-right:7px;
          border:2px solid #43c98a; border-right-color:transparent;
          border-radius:50%; animation:s .7s linear infinite;
          vertical-align:-1px; }
  @keyframes s { to { transform:rotate(360deg); } }
</style></head><body><div class="wrap">
  <h1>Start Reachy</h1>
  <div class="sub">Where is the K11 receiver plugged in?</div>

  <div id="rec" class="rec">Looking…</div>

  <div class="modes">
    <button class="mode" id="m-robot" onclick="start('robot')">
      <b>Robot mode</b><span>Mic in the robot. App runs on the robot;
      the laptop can be off-stage.</span></button>
    <button class="mode" id="m-laptop" onclick="start('laptop')">
      <b>Laptop mode</b><span>Mic in this laptop. The original setup —
      the laptop listens and streams.</span></button>
  </div>

  <div class="actions">
    <button class="act" id="rescan" onclick="rescan()">Rescan</button>
    <button class="act" id="stop" onclick="stopAll()">Stop everything</button>
  </div>

  <div id="msg" class="msg"></div>

  <div class="card" style="margin-top:18px">
    <div class="row"><span class="k">This laptop</span>
      <span class="v" id="d-laptop">—</span></div>
    <div class="row"><span class="k">Robot</span>
      <span class="v" id="d-robot">—</span></div>
    <div class="row"><span class="k">Mic on the robot</span>
      <span class="v" id="d-rmic">—</span></div>
    <div class="row"><span class="k">Mic on the laptop</span>
      <span class="v" id="d-lmic">—</span></div>
    <div class="row"><span class="k">Running</span>
      <span class="v" id="d-run">nothing</span></div>
  </div>
</div>
<script>
function micText(m) {
  if (!m) return '—';
  switch (m.status) {
    case 'live':   return '<span class="ok">live, ' +
                          m.peak_dbfs.toFixed(0) + ' dBFS peak</span>';
    case 'silent': return '<span class="warn">plugged in, no signal ' +
                          '(transmitter off?)</span>';
    case 'absent': return '<span class="bad">not plugged in</span>';
    case 'no_robot': return '—';
    case 'error':  return '<span class="bad">error: ' +
                          (m.detail || '').slice(0, 80) + '</span>';
    default:       return '—';
  }
}
async function refresh() {
  const s = await (await fetch('/api/state')).json();
  document.getElementById('d-laptop').textContent = s.laptop_ip || '—';
  document.getElementById('d-robot').innerHTML = s.robot
    ? '<span class="ok">' + s.robot.ip + '</span> (' + (s.robot.state||'?') + ')'
    : '<span class="bad">not found</span>';
  document.getElementById('d-rmic').innerHTML = micText(s.robot_mic);
  document.getElementById('d-lmic').innerHTML = micText(s.laptop_mic);
  // Both boards, not just the dashboard: show mode is reached at /show, which
  // is not a path anyone recalls under pressure.
  document.getElementById('d-run').innerHTML = s.running
    ? '<span class="ok">' + s.running + ' mode</span>' +
      (s.dashboard_url
        ? ' — <a href="' + s.dashboard_url + '" target="_blank">dashboard</a>' +
          ' · <a href="' + s.dashboard_url.replace(/\/$/, '') + '/show" target="_blank">show board</a>'
        : '')
    : 'nothing';

  const rec = document.getElementById('rec');
  const busy = s.scanning || s.busy;
  if (s.scanning) { rec.className = 'rec'; rec.innerHTML = '<span class="spin"></span>Scanning…'; }
  else {
    rec.className = 'rec' + (s.recommendation.mode ? '' : ' none');
    rec.textContent = s.recommendation.why;
  }
  for (const m of ['robot','laptop']) {
    const b = document.getElementById('m-'+m);
    b.classList.toggle('pick', !busy && s.recommendation.mode === m);
    b.disabled = busy;
  }
  document.getElementById('rescan').disabled = busy;
  document.getElementById('stop').disabled = busy;
  const msg = document.getElementById('msg');
  msg.textContent = s.message || '';
  msg.className = 'msg' + (s.message ? ' show' : '');
}
async function start(mode) { await fetch('/api/start?mode='+mode, {method:'POST'}); refresh(); }
async function rescan()    { await fetch('/api/rescan', {method:'POST'}); refresh(); }
async function stopAll()   { await fetch('/api/stop', {method:'POST'}); refresh(); }
refresh(); setInterval(refresh, 1000);
</script></body></html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a) -> None:   # quiet; this is a UI, not a server
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._send(200, json.dumps(STATE.snapshot()).encode(),
                       "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        u = urllib.parse.urlparse(self.path)
        path, q = u.path, urllib.parse.parse_qs(u.query)
        if path == "/api/rescan":
            threading.Thread(target=rescan, daemon=True).start()
        elif path == "/api/start":
            mode = (q.get("mode") or ["robot"])[0]
            if mode in ("robot", "laptop"):
                threading.Thread(target=do_start, args=(mode,),
                                 daemon=True).start()
        elif path == "/api/stop":
            threading.Thread(target=do_stop, daemon=True).start()
        else:
            self._send(404, b"not found", "text/plain")
            return
        self._send(200, b'{"ok":true}', "application/json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=LAUNCHER_PORT)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    if not VENV_PYTHON.is_file():
        print(f"venv python not found at {VENV_PYTHON}")
        return 1

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Launcher on {url}")
    print("Leave this window open — closing it stops the launcher (but not a")
    print("conversation already running on the robot).")

    threading.Thread(target=rescan, daemon=True).start()
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nLauncher closed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
