# Talking to Reachy Mini

Reachy Mini holds a Hebrew voice conversation. You speak, Gemini answers, and
the robot speaks the reply while moving.

## One robot, or ten

This app serves **one robot**. That has not changed and should not — a
conversation belongs to a body. What changed on 2026-09-17 is that it now says
*which* body, so a lab with ten Reachys runs ten of these, each with its own
dashboard, its own transcript and its own character.

The fleet is arranged by the hub in `../robot-hub`, which discovers the robots,
holds the names and the API keys, and starts one of these per robot. See
[docs/HUB-INTERFACE.md](docs/HUB-INTERFACE.md) for exactly what it passes.

Each robot runs its own copy, on itself:

```
ssh pollen@<robot-ip>
cd /home/pollen/reachy_chat && /venvs/mini_daemon/bin/python laptop_chat.py \
    --local-robot --host 0.0.0.0 --no-browser --robot-name Rina
```

and its dashboard is at `http://<robot-ip>:8765/`. Deploy to all of them at
once with `python tools/deploy_robot_app.py --all` (see `robots.json`, and
`fleet.py` for its shape).

## The persona switch

Every robot starts on the **base persona** — the friendly Hebrew desk robot it
has always been. On each dashboard there is one switch. Off is base. Flick it
on and a short panel opens where anyone can type who that Reachy should be —
a cowboy, a robot that only tells jokes — pick a voice, and save. Flicking the
switch back off returns that robot to base, which is why it is also the reset.

What is typed is layered *on top of* the base prompt and never replaces it, so
a participant cannot accidentally take the robot out of Hebrew or break its
motion tools. A persona survives a restart, and the dashboard always shows
what is actually running.

## Choosing the brain, the language and the voice

Above **Start Conversation** there is a small picker. Like the persona, it
applies to the *next* conversation and survives a restart (`backend.json`).

| Control | Options |
|---|---|
| Brain | **Gemini 3.8 Live** (default, Google's current model) · Gemini 3.1 Flash Live (previous) · **OpenAI GPT-Live-1** |
| Language | עברית · English · **Auto** (replies in whichever of the two you just spoke; Hebrew when unsure) |
| ElevenLabs v4 voice | Off, or on with a voice from the ElevenLabs account. The brain still listens, thinks and gestures; the robot speaks with ElevenLabs (`eleven_v4_turbo`). If ElevenLabs cannot be reached, that turn falls back to the brain's own voice and the log says why. |

**Keys.** Gemini's is found as before. OpenAI and ElevenLabs go in
`keys.json` next to this file (git-ignored), or in `OPENAI_API_KEY` /
`ELEVENLABS_API_KEY`:

```json
{"keys": [
  {"id": "openai",     "provider": "gpt_live",   "label": "OpenAI",     "key": "sk-..."},
  {"id": "elevenlabs", "provider": "elevenlabs", "label": "ElevenLabs", "key": "..."}
]}
```

A brain without a key says so in the dropdown, and Start refuses with a
sentence rather than half-starting. Keys are read when Start is pressed, so a
key added mid-session works without a restart. In robot mode the launcher
passes every key the laptop has into the robot app's environment.

**GPT-Live-1 is different in kind**, and it is worth knowing how before the
test (details in `providers/gpt_live.py`): it hears you live while you speak
and decides itself when you have finished; it has no motion tools (the robot
still sways to its own speech); and **its Hebrew is unverified** — OpenAI
publishes no language list and all its voices are English or Portuguese.

**Test without the robot first.**

```powershell
.venv\Scripts\python.exe tools\check_backends.py          # every combination you have keys for
.venv\Scripts\python.exe tools\dry_run.py                 # the real dashboard; laptop mic + speakers are the robot
```

`check_backends.py` holds a three-turn conversation on each brain × language
(× ElevenLabs) from recorded speech and prints PASS/FAIL with what was heard,
what was said and the latency. `tools/test_fake_backends.py` exercises the
GPT-Live and ElevenLabs plumbing against local stand-ins, needing no keys.

## The microphone, and what is not yet measured

The robot's built-in microphone is what listens now. **The K11 lavalier is no
longer used**, and the values that described it are stale: `--mic-match` still
defaults to the K11's device name, and nobody has yet measured what the
built-in mic enumerates as or what rate it opens at.

Run `tools/probe_internal_mic.py` on a robot before trusting the audio path.
It prints the exact `--mic-match` and `--mic-rate` to launch with, and — more
importantly — measures **how loudly the robot hears itself**. The mic now sits
in the head, inches from the robot's own speaker and its servos, which the
lavalier never did. That number decides whether barge-in is reachable at all.

---

## The old single-robot setup

Everything below describes the original laptop-plus-lavalier arrangement. It
still works, and it is still the fallback, but it is not how a fleet runs.

The robot's own microphone was broken on the first unit, so the listening was
done through the **K11 lavalier** instead. Which machine you plug its receiver
into decides how everything runs:

| | **Laptop mode** | **Robot mode** |
|---|---|---|
| K11 receiver in | the laptop | the robot |
| App runs on | the laptop | the robot |
| Audio to the robot | over SSH | a local pipe |
| Dashboard at | `127.0.0.1:8765` | `<robot-ip>:8765`, opened on the laptop |
| Laptop must be | near you, listening | anywhere on the same network |

Robot mode exists so the laptop can be off-stage during the lecture. It is not
a rewrite — same turn loop, same dashboard, same wire protocol to the same
player. Laptop mode is unchanged and stays the fallback.

## Just start it

From the `reachy-mini` folder (the one above this):

```powershell
.\start.ps1
```

That finds the robot on whatever network you are on, works out which machine
the K11 is plugged into and whether it is actually sending signal, and opens a
page with the right mode preselected. Press Start. No IP to look up.

It also stops anything already running first — important, because two
instances fight over the robot's audio and the symptom looks exactly like a
broken mic.

Everything below is the same thing done by hand.

## The charging conflict — read this before a live demo

**The K11 receiver blocks the charging port**, on the robot and on the laptop
both. Whichever machine is doing the listening therefore runs on battery for
the whole session.

That is worse on the robot than it sounds, because the robot reports **no
battery level anywhere** — not in `/sys/class/power_supply/`, not in the
daemon's status API. There is no warning. It stops mid-sentence and drops off
the network, which is exactly how the 2026-07-27 session ended.

A short USB extension cable for the receiver removes the conflict entirely and
is worth buying before the lecture. Until then:

- Charge the robot fully immediately before the talk, and treat the runtime as
  unknown rather than assumed.
- If the talk is long, **laptop mode is the safer choice** — a laptop battery
  lasts hours and tells you what it has left.
- `.\start.ps1` will report the mic as "not plugged in" while the robot is
  charging. That is the conflict, not a fault.

## Before you start

1. **Robot powered on**, and joined to the **same WiFi as this laptop**. A phone
   hotspot works only if the laptop is on that hotspot too.
2. **Lavalier mic**: USB receiver plugged into the laptop, transmitter switched
   on, clipped near your mouth, not muted.
3. Close the Reachy Mini desktop app if it is open — it grabs the robot's audio
   and this app will not get it.

## Run a conversation

Open PowerShell and run these, in order.

```powershell
cd "$HOME\Desktop\job\reachy-mini\reachy_chat"
```

**Step 1 — check everything is ready.** Verifies the mic is actually sending
audio, the robot is reachable, and the API key resolves.

```powershell
.\.venv\Scripts\python.exe tools\preflight.py
```

Fix anything it marks `FAIL` and run it again. When it passes it prints the
exact command for step 2.

**Step 2 — start the app.**

```powershell
.\.venv\Scripts\python.exe laptop_chat.py --robot-host 10.100.102.18
```

Your browser opens `http://127.0.0.1:8765` automatically.

**Step 3 — in the browser.**

1. Wait for **System Status: Idle** (green dot). Takes about 10 seconds — the
   robot starts breathing when it is ready.
2. Click **Start Conversation**.
3. Talk. Pause when you finish a sentence; after 0.8 s of silence Reachy
   answers, about 3 seconds later.
4. To finish, click **End Conversation** or say `להתראות` / `סיים שיחה` /
   `goodbye`.

**Step 4 — shut down.** Press `Ctrl-C` in the PowerShell window. The robot
closes cleanly.

## Show mode — you drive, Reachy performs

For a lecture or a stage, where a live conversation is too risky. You press
keys, Reachy speaks pre-recorded Hebrew lines and moves. Instant, identical
every time, and **it needs no internet** — only the local link to the robot.

### Editing the script from the board

Every cue card has an **edit** button in its top-right corner. It opens the
line for rewriting, and **Save re-records it** — a few seconds per line, no
terminal, no JSON. `+ add cue` at the bottom of a section creates a new one;
**Delete** inside the editor removes a cue and its recording.

The editor also covers the hotkey, the motion (picked from the emotions and
dances the robot actually has, so a typo can't reach the stage), and the boss's
cue line. `docs\SHOW_SCRIPT.md` is regenerated on every save, so the printed
sheet never drifts from what Reachy will say.

**Rebuild audio** in the header re-records anything whose text changed —
useful after editing `cues.json` by hand, or if a recording failed.

Clicking the card itself still fires the cue; only the small edit button opens
the editor. Every save writes a timestamped backup to `show\backups\`, keeping
the last 30.

### Editing the script by hand

`show/cues.json` is still a plain file, and the terminal path still works —
the two are interchangeable and produce identical audio:

```powershell
.\.venv\Scripts\python.exe tools\build_show.py
```

That regenerates only the lines you changed, and rewrites the boss's cue sheet
at `docs\SHOW_SCRIPT.md` — print that for whoever is speaking. Press **Reload
script** on the board afterwards to pick up hand edits without restarting.

Start the app as usual, then open the operator board in a second tab:

```
http://127.0.0.1:8765/show
```

Keep that tab on the laptop screen only — never on the projector.

| Key | Does |
|---|---|
| `Space` | Fire the outlined cue and advance to the next one |
| `↑` `↓` | Move the outline without firing |
| `Esc` | STOP — silence Reachy immediately, back to breathing |
| number/letter keys | Fire that cue directly, out of order (the reactions) |

Cues with text speak; cues marked *motion only* just move, so they're safe to
fire at any moment — including while Reachy is talking. The three **SAVE** cues
(`s` `d` `f`) exist for when a live conversation stalls: "hold on a second",
"say that again", "nice talking to you". They interrupt whatever is playing.

Both modes share one robot connection, so you can switch between conversation
and cues without restarting anything.

## Running it on the robot by hand

`start.ps1` does all of this for you; this is what it runs.

```powershell
# 1. push the app (incremental; --with-show is needed for the cue board)
.\.venv\Scripts\python.exe tools\deploy_robot_app.py --with-show

# 2. start it there
ssh pollen@<robot-ip>
cd /home/pollen/reachy_chat
/venvs/mini_daemon/bin/python laptop_chat.py --local-robot --host 0.0.0.0 --no-browser
```

Then open `http://<robot-ip>:8765` in the laptop's browser. `--host 0.0.0.0` is
what makes it reachable from off the robot; `--local-robot` is what switches
the mic, the transport, and the VAD backend.

To stop it: `pkill -f '[l]aptop_chat.py'` on the robot. (The `[l]` is
deliberate — without it, `pkill -f` matches the shell running the pkill.)

The robot's log is `/tmp/reachy_app.log`, and per-run logs land in
`/home/pollen/reachy_chat/system_logs/` exactly as they do on the laptop.

### What differs in robot mode, and why

- **Mic.** The same K11 receiver enumerates as `USB Composite Device` on the
  robot, not `USBAudio1.0`, and raw ALSA offers **48 kHz only** — opening it at
  16 kHz fails outright. So capture runs at 48 k and is downsampled 3:1.
- **Gain.** Raw ALSA gives no equivalent of the boost Windows applies, so
  capture is multiplied by 3 to land at the levels every VAD and energy-gate
  threshold was tuned against. Without it, quiet speech drifts under
  `MIN_PEAK_FLOOR` and turns get silently rejected.
- **VAD.** The robot has no PyTorch and should not get it — it is ~1 GB against
  ~3.7 GB free on the SD card. `vad_onnx.py` runs the same model under
  `onnxruntime`, which is already installed. `tools/test_vad_parity.py` checks
  the two backends against each other; they currently agree exactly.
- **Transport.** No SSH hop. `local_transport.py` wraps a child process in the
  same interface paramiko's channel exposes, so the framing, sentinels and
  shutdown path are shared code, not a second implementation.

## If the robot moved to a different address

Its IP changes when it joins a different WiFi. Find it:

```powershell
.\.venv\Scripts\python.exe tools\find_robot.py
```

This scans the laptop's own network and prints the ready-to-paste command. Use
the address it reports with `--robot-host`. Don't rely on `reachy-mini.local` —
on this laptop it is stale-pinned to an old hotspot address.

## When something is wrong

| What you see | What to do |
|---|---|
| Preflight says the mic is silent | Transmitter off, muted, or unpaired from the receiver. Switch it on and re-run. |
| Preflight says no device matches `USBAudio` | The USB receiver is unplugged. Plug it back in — do not run without it, the laptop's built-in mic hears the room and Reachy's own voice. |
| Preflight cannot reach the robot | Robot off, or on a different WiFi. Check the network, then `tools\find_robot.py`. |
| Reachy moves but never speaks | Something is holding the robot's audio — usually the desktop app. Close it and restart this one. |
| Reachy answers twice | Known Gemini behaviour, guarded against; tell Claude and check `tool.dispatched` in the run's `events.jsonl`. |
| Status stuck on Starting | The robot is not answering SSH. `Ctrl-C`, run preflight, start again. |
| Preflight says cue text changed | You edited `show\cues.json` but didn't regenerate. Run `tools\build_show.py`. |
| A cue says the wrong (old) line | Same cause — the audio on disk is from the previous wording. |
| Launcher says the mic is "plugged in, no signal" | The receiver is in, but the transmitter is off, muted, or unpaired. Switch it on and Rescan. This reads as digital silence, which looks identical to a working mic from the outside. |
| Launcher finds no robot | Robot off, still booting, or on a different network from this laptop. |
| Robot mode: "address already in use" | An instance is already running on the robot. Press **Stop everything** in the launcher, or `pkill -f '[l]aptop_chat.py'`. |
| Robot mode: show board is empty | The cue WAVs were never pushed. Re-deploy with `--with-show`. |

## Where the conversation is saved

Every run writes a folder under `conversations/`, named by start time:

```
conversations\2026-07-26_18-49-01\
    transcript.txt   what was said, both sides
    summary.json     per-turn timings, tool calls, anomalies
    turn_001.wav     Reachy's audio, one file per turn
    events.jsonl     full event stream
    laptop.log       full log for that conversation
```

Nothing is ever overwritten. To read the latest one:

```powershell
$c = Get-ChildItem conversations | Sort-Object Name | Select-Object -Last 1
Get-Content "$($c.FullName)\transcript.txt"
```

## Deeper documentation

`docs/ARCHITECTURE.md` — how it works: the streaming pipeline, the wire
protocol to the robot, motion layers, tuning knobs, and the full tools list.
`docs/` also holds the dashboard design docs.
