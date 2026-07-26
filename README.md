# Talking to Reachy Mini

Reachy Mini holds a Hebrew voice conversation. You speak into the lavalier mic
clipped to you, Gemini answers, and the robot speaks the reply while moving.

The robot's own microphone is broken, so **the laptop does the listening**.
That is the whole reason this app exists.

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
cd "C:\Users\tomer\Desktop\job\reachy-mini\reachy_chat"
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

The script lives in `show/cues.json`. After editing any line:

```powershell
.\.venv\Scripts\python.exe tools\build_show.py
```

That regenerates only the lines you changed, and rewrites the boss's cue sheet
at `docs\SHOW_SCRIPT.md` — print that for whoever is speaking.

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
