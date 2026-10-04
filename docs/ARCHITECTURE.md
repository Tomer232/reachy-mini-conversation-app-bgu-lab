# Reachy Mini × Gemini — architecture

**To actually run a conversation, see `../README.md`.** This document is the
deep reference: how the pipeline works, the wire protocol, the motion layers,
the tuning knobs, and the phase-by-phase history.

Laptop captures your voice with Silero VAD endpointing, holds one Gemini Live
session open across all turns, and **streams** Gemini's audio chunks down an
SSH channel to a **long-running** robot process that plays them as they
arrive. Robot SDK init (~5 s) happens once per conversation, not per turn.

Hebrew is the default language. End-phrases are matched in both Hebrew and
English.

## Folder layout

```
reachy_chat/
├── README.md                  runbook: how to hold a conversation
├── laptop_chat.py             entry point — starts the dashboard server
├── system.py                  SystemManager: lifetime resources + system FSM
├── conversation.py            config, VAD capture, SSH player, drain loop, turn loop
├── robot_streaming_player.py  source of the long-running robot script
│                              (audio + 100 Hz motion loop + tapper)
├── robot_speech_tapper.py     verbatim port of Pollen's SwayRollRT DSP
├── robot_play.py              Phase-1 one-shot player (kept as fallback)
├── vad.py                     Silero wrapper
├── event_log.py               events.jsonl writer
├── logging_setup.py           logger wiring, redaction, per-conversation log
├── summary.py                 events.jsonl -> summary.json
├── web/                       FastAPI app, WebSocket broadcaster, dashboard UI
├── docs/                      this file + the dashboard design docs
├── conversations/             one subfolder per run, named by start timestamp
│   └── 2026-05-18_15-48-01/
│       ├── transcript.txt     appended line by line as turns complete
│       ├── timings.csv        per-turn rows with a header
│       ├── events.jsonl       structured event stream
│       ├── summary.json       per-turn timings, tool calls, anomalies
│       └── turn_001.wav …     Gemini's response audio (24 kHz, native rate)
├── system_logs/               one log per process start (idle-time events)
├── tools/                     standalone helpers — diagnostics, deploys, benchmarks
├── archive/                   historical artifacts, prior backups, benchmark output
└── .venv/                     Python 3.12 venv (unchanged)
```

The `conversations/` folder grows over time — every run of `laptop_chat.py`
creates a new timestamped subfolder. Nothing is overwritten between runs.
Per-turn artifacts (`transcript.txt`, `timings.csv`, `turn_NNN.wav`) live
exclusively in that folder.

`archive/` holds: the original `laptop_chat.py` backups, the Phase 1+2
`REPORT.md`, the model + voice benchmark output, the Hebrew TTS sample WAVs,
the legacy `timings_phase1.csv`, and previous-architecture prototype
scripts (`reachy_chat.py`, `sdk_audio_test.py`, …).

## One-time setup

### Robot

Deploy the streaming player:

```powershell
python tools\deploy_robot_player.py
```

Verify the daemon is running:

```bash
sudo systemctl restart reachy-mini-daemon
```

### Laptop

```powershell
cd "C:\Users\tomer\Desktop\job\reachy-mini\reachy_chat"
.\.venv\Scripts\Activate.ps1
pip install google-genai sounddevice paramiko soundfile scipy numpy silero-vad fastapi "uvicorn[standard]"
```

API key auto-resolution order:
1. `GEMINI_API_KEY` env var
2. `.gemini_key` next to `laptop_chat.py`
3. `C:\Users\tomer\Desktop\job\reachy-mini\reachy-mini llm gemini token.txt`

Robot host auto-resolution order:
1. `--robot-host` CLI arg
2. `REACHY_ROBOT_HOST` env var
3. `ROBOT_HOST_DEFAULT` in `conversation.py` (currently `10.100.102.18`)

The resolved host and which tier won are logged at startup, and it is the
single source of truth for the SSH connection, the dashboard's status panel,
and the daemon heartbeat URL. `tools/find_robot.py` scans the laptop's own /24
when the robot has moved.

```powershell
$env:REACHY_ROBOT_HOST = "10.100.102.18"   # or: python laptop_chat.py --robot-host 10.100.102.18
```

Input device: `INPUT_DEVICE = "USBAudio"` pins the K11 lavalier receiver, which
enumerates as the generic `Microphone (USBAudio1.0)`. `INPUT_HOSTAPI_PREFERENCE`
puts MME first because that entry accepts 16000 Hz mono through the OS
resampler, which keeps the capture blocksize tied to `SILERO_FRAME_SIZE`. If no
device matches, capture silently falls back to the system default — which is
why `tools/preflight.py` treats that fallback as a failure.

## Run

```powershell
.\.venv\Scripts\python.exe laptop_chat.py
```

This launches the web dashboard at http://127.0.0.1:8765 (it opens in your
browser automatically) and brings the system up to idle with the robot
breathing. Use the dashboard to Start/Stop the system and Start/End
conversations; the live transcript, event log, and robot status appear there.
Each conversation writes a folder under `conversations/`, for example:

```
conversations\2026-05-18_15-48-01
```

## How a turn feels

1. 0.5 s ambient calibration, SSH to the robot, wait for `Robot ready in X.XXs` (~5 s) — paid **once**.
2. `>>> מקשיב (דבר עכשיו) / Listening (speak now)…`
3. You start talking. `>>> שומע אותך / Hearing you.` appears the moment Silero VAD catches the first speech frame.
4. You stop talking. After **0.8 s of silence** the turn ends.
5. Audio is sent over the persistent Gemini Live session.
6. As each Gemini chunk arrives, it's resampled 24 kHz → 16 kHz and pushed down the SSH channel. The robot starts speaking ~2-3 s after you stopped (vs ~16 s in Phase 1).
7. The user→robot exchange is appended to `conversations/<timestamp>/transcript.txt` *immediately*, so a Ctrl-C leaves a partial record.
8. Loop to step 2. No reconnects, no per-turn init.

### Ending the conversation

Say one of:
- Hebrew: `להתראות`, `סיים שיחה`, `תסיים שיחה`, `ביי`, `תפסיק`, `תפסיקי`
- English: `goodbye`, `end conversation`, `stop`

Or Ctrl-C.

On exit the laptop closes its end of the SSH channel; the robot process
sees EOF on stdin, calls `stop_playing()` and `mini.close()`, and exits
with rc=0.

## Tunables (top of `laptop_chat.py`)

Model and voice:
- `GEMINI_MODEL` — `gemini-3.1-flash-live-preview`
- `GEMINI_VOICE` — `Aoede` (or `Kore` / `Charon` / `Puck` / `Leda` — listen to `archive/voice_*.wav`)
- `GEMINI_LANGUAGE_CODE` — `he-IL`

Streaming:
- `ROBOT_OUTPUT_RATE` — 16 000
- `RESAMPLE_BATCH_24K` — 1 536 samples (64 ms @ 24 kHz; multiple of 3 for clean 2:3 resampling)
- `ROBOT_READY_TIMEOUT_S` — 30

VAD: `VAD_AGGRESSIVENESS=2`, `SILENCE_HANGOVER_S=0.8`, `MIN_SPEECH_S=0.3`, etc.

## Per-turn timings (CSV schema)

```
turn, mic_record_s, vad_to_send_s, gemini_first_chunk_s,
time_to_first_audio_to_robot_s, gemini_total_s, audio_duration_s,
streaming_overhead_s, wall_clock_s, user_chars, asst_chars
```

Log line example:
```
[turn 2] mic_record=2.10s vad_to_send=0.002s gemini_first_chunk=2.35s
         first_to_robot=2.47s gemini_total=8.49s audio_dur=6.04s
         stream_overhead=+2.45s wall=8.49s
```

Headline metric: perceived gap (silence → robot starts) ≈ `vad_to_send + time_to_first_audio_to_robot` ≈ 2-3 s.

## Wire protocol (laptop → robot)

Each message on the SSH channel's stdin is `[4-byte big-endian uint32 length][payload]`:

| length          | meaning                                                           |
|----------------:|-------------------------------------------------------------------|
| `0x00000000`    | end-of-turn marker (empty payload) — robot logs it on stderr      |
| `0xFFFFFFFC`    | **Phase 3B** motion command — UNIQUE: followed by a second 4-byte `uint32` payload length, then UTF-8 JSON. See "Phase 3B" section below. |
| `0xFFFFFFFD`    | listening-end (Phase 3A; user just stopped speaking) — robot logs |
| `0xFFFFFFFE`    | listening-start (Phase 3A; user just started speaking) — robot logs |
| `0xFFFFFFFF`    | clear playback buffer (empty payload) — mid-turn interrupt        |
| 1 .. 2³² - 5    | audio chunk; payload = `float32` PCM at 16 kHz mono               |

EOF on stdin ⇒ end of conversation. Robot calls `stop_playing()`, closes
the `ReachyMini` context, exits rc=0.

## Tools (under `tools/`)

| Script | Purpose |
|---|---|
| `preflight.py` | **Run this before a conversation.** Checks the lavalier is sending audio (a matched-but-silent receiver and an unmatched device both fail), the robot answers on ports 22 and 8000, and the API key resolves. Prints the launch command. |
| `find_robot.py` | Scan the laptop's own /24 for the robot daemon and identify it via `/api/daemon/status`. Use when the robot changed networks. |
| `mic_check.py` | Capture 3 s from the resolved input device, report RMS/peak in dBFS, save a WAV. Isolates the receiver from VAD/Gemini/robot. |
| `probe_mic.py` | Full sounddevice device table plus a 16k/44.1k/48k mono support matrix per input. |
| `deploy_robot_player.py` | SFTP `robot_streaming_player.py` to the robot and verify it compiles. |
| `test_streaming_robot.py` | Standalone sine-wave test of the robot player (no Gemini, no mic). |
| `test_vad.py` | Offline Silero VAD sanity check against `archive/bench_input_he.wav`. |
| `test_mic.py` | Confirms `sounddevice` InputStream delivers 16 kHz / 320-sample frames. |
| `smoke_streaming.py` | Phase 2 smoke — 2 Gemini turns through the streaming pipeline (no mic); outputs to `archive/smoke_streaming/`. |
| `smoke_e2e.py` | **Historical** Phase 1 smoke; targets the old `RobotPlayer.upload/play` API and will raise AttributeError under Phase 2 — kept for reference. |
| `make_he_sample.py` | Synthesise the Hebrew TTS sample used by benchmarks; writes to `archive/`. |
| `benchmark_models.py` | Head-to-head Gemini Live model latency comparison; reads `archive/bench_input_he.wav`, writes `archive/model_benchmark.{md,json}`. |
| `benchmark_voices.py` | Per-voice Hebrew quality sample; writes `archive/voice_*.wav`. |
| `list_models.py` | Print models visible to the API key, with bidi support flagged. |
| `probe_genai.py` | Introspect the `google.genai` types we depend on. |
| `probe_robot.py` | SSH-level liveness check for the robot daemon and helper script. |
| `probe_robot_audio.py` | Introspect `mini.media` / `mini.media.audio` to confirm method names. |
| `probe_robot_motion.py` | Phase 3A — verify `set_target` / `create_head_pose` / `compose_world_offset` / `linear_pose_interpolation` are available. |
| `test_breathing.py` | Phase 3A — exec the new player, send no audio for 30 s, watch the robot breathe; reports motion-loop frequency stats at shutdown. |
| `list_moves.py` | Phase 3B — regenerate `archive/move_catalog.md` from the installed `RecordedMoves` + `reachy_mini_dances_library` libraries. |
| `test_motion_command.py` | Phase 3B — exec the new player, send a fixed sequence of `play_emotion` / `dance` / `move_head` / `stop` commands directly (no Gemini, no mic). Watch the robot perform each one. |
| `start_reachy.py` | **The launcher.** Discovers the robot, probes for the K11 on both machines, serves the mode-choice page, enforces a single instance, starts the chosen mode. Invoked by `..\start.ps1`. |
| `deploy_robot_app.py` | Push the whole app to `/home/pollen/reachy_chat/` for robot mode (incremental; `--with-show` adds the cue WAVs; `--check` verifies without copying). Verifies every module imports under the robot's venv. |
| `test_vad_parity.py` | Check the ONNX VAD on the robot against the torch VAD on the laptop, frame by frame. Run after touching either. |
| `test_show_editor.py` | Round-trip the cue editor: add, edit (re-records), delete, and assert `cues.json` comes back identical. `--no-tts` skips synthesis. |
| `exp_session_reopen.py` | Live-API experiment behind the silence handling: does silence get a reply, does it poison the session, does a reopen recover. |
| `test_local_player.py` | Robot-side twin of `test_streaming_robot.py`: drives the player as a child process. Two beeps and a head turn, no Gemini, no mic. |

## When this might fail

- **Robot never says "ready"** — `tools/test_streaming_robot.py` times out after 30 s. SSH in and inspect `~/scripts/robot_streaming_player.py` and the daemon.
- **Audio sounds buzzy / glitchy** — see "Audio artifacts" in `archive/REPORT.md`. Likely fix is a stateful overlap-save resampler; not currently implemented.
- **Robot dropped from WiFi** — `Test-NetConnection 10.100.102.18 -Port 22`. The robot is intermittent on home WiFi.
- **Robot mic** — broken hardware; not used.

## Restoring previous versions

```powershell
# Phase 1 final
Copy-Item archive\laptop_chat_phase2.py.bak2 laptop_chat.py -Force

# Pre-Phase 1 original
Copy-Item archive\laptop_chat_phase1.py.bak laptop_chat.py -Force
```

The Phase 1 path also requires `~/scripts/robot_play.py` on the robot,
which is still in place.

## Phase 3A — idle breathing + speech-reactive head motion

The robot is no longer a talking statue. The robot-side player now runs
three threads in one Python process:

1. **StdinReader** (main) — reads framed messages, decodes audio, feeds
   the speech tapper, and hands samples to the audio queue.
2. **AudioPlayer** — pulls float32 frames from the queue and calls
   `mini.media.push_audio_sample`.
3. **MotionLoop** (new) — runs at 100 Hz, evaluates the current primary
   `Move` plus secondary offsets, and calls `mini.set_target`.

### The three motion layers

| Layer | Source | Phase 3A | Phase 3B/C (future) |
|---|---|---|---|
| **Primary move** | `BreathingMove` only — gentle z-axis sway + antenna sway, runs forever once started. | ✅ | Will become a sequential queue (emotions, gotos, dances). |
| **Secondary offsets** | `SwayRollRT` (vendored from Pollen) consumes the audio stream and emits per-50-ms hop dicts of (x, y, z, roll, pitch, yaw). Motion loop reads the freshest hop each tick and composes it onto the primary head pose via `compose_world_offset`. | ✅ | Could add face-tracking offsets as a second additive source. |
| **LLM-driven dances / emotions** | Tool-calling triggers from the model. | ❌ (out of scope for 3A) | Wire dance library + emotion primitives behind a queue. |

### `robot_speech_tapper.py`

Verbatim port of Pollen's
[`audio/speech_tapper.py`](https://github.com/pollen-robotics/reachy_mini_conversation_app/blob/main/src/reachy_mini_conversation_app/audio/speech_tapper.py).
All tunables kept as upstream. The tapper produces a dict per 50 ms hop
with both radian (pitch / yaw / roll) and millimetre (x / y / z) offsets;
the player converts mm → m before composing.

### New optional sentinels in the wire protocol

The protocol now recognises two additional empty-payload sentinels:
`0xFFFFFFFE` (listening-start) and `0xFFFFFFFD` (listening-end). The
laptop sends them around `record_with_vad` so the robot can distinguish
"user is speaking" from "Gemini is speaking". In Phase 3A the robot
**logs them but doesn't change behaviour** — the primary move is always
`BreathingMove`, and the audio-reactive sway already runs whenever audio
is flowing. They're laid down now so Phase 3B can wire an "attentive
listening" pose with no protocol change.

### Motion-loop frequency

The motion loop targets 100 Hz. Measured numbers on the current robot:

- Breathing-only (no audio): **count=3003, mean_hz=100.48, min_hz=65.10**
- With audio + tapper running: **count=1967, mean_hz=98.33, min_hz=6.70**

The ~2 Hz drop with audio is the extra per-tick work of building the
secondary `create_head_pose` and composing via `compose_world_offset` —
it's expected. `min_hz` dips are isolated to the first one or two ticks
during SDK warm-up. The numbers are printed on stderr at robot shutdown
as `motion_loop_stats: count=… mean_hz=… min_hz=…`.

### Tunables (in `robot_speech_tapper.py`)

The defaults are Pollen's. The ones most worth tweaking by ear:

- `SWAY_MASTER` (1.5) — overall amplitude scale for ALL six sway axes
- `SWAY_A_PITCH_DEG` / `SWAY_A_YAW_DEG` / `SWAY_A_ROLL_DEG` (4.5°, 7.5°, 2.25°)
- `SWAY_A_X_MM` / `SWAY_A_Y_MM` / `SWAY_A_Z_MM` (4.5, 3.75, 2.25 mm)
- `SWAY_DB_LOW` / `SWAY_DB_HIGH` (-46 / -18 dB) — the loudness range mapped
  onto [0, 1]; lower `SWAY_DB_LOW` makes the head sway more vigorously at
  whisper volumes.

In `robot_streaming_player.py`'s `BreathingMove`:
- `breathing_z_amplitude` (0.005 m, i.e. 5 mm)
- `breathing_frequency` (0.1 Hz, i.e. 6 breaths / min)
- `antenna_sway_amplitude` (15°)
- `antenna_frequency` (0.5 Hz)

### How to verify

1. **Breathing only.** `python tools\test_breathing.py` — robot does the
   gentle z-bob and antenna sway for 30 s with no audio. Confirms primary
   motion is working in isolation.
2. **Live conversation.** `python laptop_chat.py` and talk. The head
   should sway in time with Gemini's voice while it speaks; the breathing
   pattern resumes between turns. Watch and listen for clicks at chunk
   boundaries — there shouldn't be any.

### Reference clone

A read-only clone of Pollen's
[`reachy_mini_conversation_app`](https://github.com/pollen-robotics/reachy_mini_conversation_app)
lives at `..\_ref_pollen_conv_app` (sibling of `reachy_chat\`). It is
**not** tracked here and can be deleted once you're done reviewing the
ports. The two files we read were
`src/reachy_mini_conversation_app/audio/speech_tapper.py` and
`src/reachy_mini_conversation_app/moves.py` (BreathingMove section).

## Phase 3B — LLM-driven emotions, dances, and head moves

Gemini now has three tools it can call mid-conversation to request a
named motion. The tool calls arrive on the same Live API stream as audio
chunks; the laptop forwards them to the robot over the existing SSH
channel as a new wire-protocol message. The motion plays **in parallel**
with voice audio (not instead of it), and the speech-reactive sway from
Phase 3A continues to layer on top.

### The three tools

| Tool | Args | What it does |
|---|---|---|
| `play_emotion(name)` | `name`: one of 81 emotion names from `pollen-robotics/reachy-mini-emotions-library` | Plays a 2–12 s pre-recorded face/head/antenna gesture. Used for affective reactions. |
| `dance(name, repeat=1)` | `name`: one of 20 dance names from `reachy_mini_dances_library`; `repeat` ∈ [1, 3] | Plays a named choreographed routine. Used sparingly, for upbeat moments. |
| `move_head(direction)` | `direction`: `left` / `right` / `up` / `down` / `front` | Linear 1-second goto to the named direction (or back to center for `front`). |

The full enum + per-move description is generated by
`tools/list_moves.py` into `archive/move_catalog.md`. The tool schema
passed to Gemini contains every option's description inline so the model
picks well.

### System-prompt addendum

The Hebrew system prompt has a one-line dampener: "use the tools when
appropriate — not in every sentence." LLMs over-trigger tools by default;
this nudge keeps motion punctual.

### Tool-response scheduling — read before touching `_handle_tool_call`

The declarations are BLOCKING (the SDK default), which means our
`FunctionResponse` is not only data: it is also the signal that lets the model
continue. The same reply therefore has two opposite effects depending on when
the call arrived, and both failure modes have been observed live:

| Call arrives | Reply with default scheduling | Reply with `SILENT` |
|---|---|---|
| **before** any audio | model was waiting on us; it now speaks — correct | model never speaks; the whole conversation is mute |
| **after** audio began | model already spoke; reads as "now answer" and it re-generates the entire reply — the robot says it twice | result is filed into context, no new generation — correct |

So `_response_scheduling(audio_started)` returns `SILENT` only once the turn
has produced audio, fed by `first_chunk_t` in the drain loop. `SILENT` can
therefore never mute a turn; the worst case is a skipped follow-up remark.

Evidence, four experiments against the live API with `archive/bench_input_he.wav`:

```
blocking + default      spoke 22/22 turns; duplicated on after-audio turns
NON_BLOCKING + SILENT   MUTE whenever the call arrived before audio
blocking + SILENT       MUTE — SILENT alone breaks it
NON_BLOCKING + default  MUTE — NON_BLOCKING alone breaks it too
```

`behavior=NON_BLOCKING` on the declarations looks like the documented fit for
fire-and-forget motion, and it is wrong here: the model emits the call and ends
the turn without speaking. A fifth experiment that held the reply until after
audio deadlocked outright, which is the cleanest demonstration of the blocking
semantics.

Live runs: `2026-07-26_17-26-01` doubled on 3 of 5 turns (unconditional
default); `2026-07-26_18-25-17` was mute on 4 of 4 (unconditional
NON_BLOCKING+SILENT); `2026-07-26_18-49-01` ran 6 turns clean under the
conditional rule — though all six calls arrived before audio, so the `SILENT`
branch is not yet exercised in the wild. The `tool.dispatched` event records
`audio_started` and the chosen scheduling per call, so the first real
occurrence is visible in `events.jsonl`.

### New wire-protocol message

`0xFFFFFFFC` (motion-command sentinel) is **the only protocol message
with a non-empty payload**, so it deviates slightly from the existing
sentinels:

```
[4-byte uint32 = 0xFFFFFFFC]
[4-byte uint32 = JSON payload length]
[N bytes of UTF-8 JSON]
```

JSON shape:

```json
{"type": "emotion", "name": "amazed1"}
{"type": "dance",   "name": "yeah_nod"}
{"type": "head",    "direction": "left"}
{"type": "stop"}
```

`stop` cancels the current move and returns the robot to `BreathingMove`
immediately. Any other type swaps the current primary move and resets
the move start time; when the new move finishes (finite duration), the
motion loop installs a fresh `BreathingMove` seeded from the current
pose — same fallback path used in Phase 3A.

If the robot receives an unknown emotion / dance / direction name it
logs the error on stderr and stays on whatever move was running. The
laptop side validates against the local enum first, so a bogus name only
gets dispatched if the catalogs drift apart.

### HF token requirement

The robot loads the emotions library from Hugging Face Hub, which
requires `HF_TOKEN`. The deploy procedure ships:

- `hf_token.txt` (laptop, project root) — 37-byte token file, read at
  startup. **Never committed.**
- `/home/pollen/.hf_token` on the robot — chmod 600, also a 37-byte
  token file. Read by `robot_streaming_player.py` before any HF import.

`tools/list_moves.py` and `laptop_chat.py` both read the laptop's token
file at startup. `robot_streaming_player.py` reads its own at startup.

### Catalog cache caveat

The first time `RecordedMoves(...)` is constructed, HF Hub does a lazy
fetch and **sometimes leaves the cache partial** (we observed 26/81 and
later 31/81 on different runs). The fix is `huggingface_hub.snapshot_download(...)` with the full repo, or — if the network is slow — SFTP
the laptop's full cache directly into
`/home/pollen/.cache/huggingface/hub/datasets--pollen-robotics--reachy-mini-emotions-library/snapshots/<sha>/`.
After this Phase 3B deploy, the robot has all 81 emotions cached.

### Quick test

```powershell
# Direct dispatch — no Gemini, no mic. Watch the robot.
python tools\test_motion_command.py
```

Plays `amazed1` → `yeah_nod` dance → 5 head directions → `curious1` →
`stop`. Motion loop frequency is reported at shutdown.

```powershell
# Full conversation — Gemini picks the tools.
python laptop_chat.py
```

Tool calls appear in the laptop log as
`[INFO] [tool] play_emotion(name=lost1) -> queued`.

## Robot mode — the whole app on the body

Added 2026-07-27, when the K11 receiver moved onto the robot's own USB. The
goal was the laptop off-stage during the lecture, not absent: it still runs the
launcher, still shows the dashboard, still fires show cues. What moves is the
audio path.

```
laptop mode   mic -> laptop [VAD -> Gemini -> resample] --SSH--> robot [player]
robot mode                   robot [VAD -> Gemini -> resample -> player]
                             laptop: browser only
```

Enabled by `laptop_chat.py --local-robot`, which sets `conversation.LOCAL_ROBOT`.
That flag is read **at call time**, never captured at import, so the CLI can
flip it after the module loads. Four things branch on it, and nothing else:

| Branch | Laptop | Robot |
|---|---|---|
| `_resolve_input_device` | `USBAudio` under MME at 16 kHz | `Composite` under ALSA at 48 kHz, `decim=3`, `gain=3.0` |
| `StreamingRobotPlayer.__init__` | paramiko channel | `local_transport.LocalPlayerChannel` |
| VAD class (in `system.py`) | `vad.SileroVAD` (torch.jit) | `vad_onnx.SileroVADOnnx` |
| `get_robot_host` fallback | `ROBOT_HOST_DEFAULT` | `127.0.0.1` |

Everything downstream — framing, sentinels, the drain loop, tool dispatch, the
listening state machine, the dashboard — is shared code taking the same path
in both modes.

### Why a channel adapter instead of a second player class

`LocalPlayerChannel` implements the eight methods `StreamingRobotPlayer` uses
from `paramiko.Channel` (`send`, `closed`, `shutdown_write`,
`recv_exit_status`, `close`, `recv_stderr_ready`, `recv_stderr`,
`exit_status_ready`) over `subprocess.Popen`. The alternative — a parallel
player implementation — would have duplicated the resampler, the framing, the
ready detection and the shutdown sequence, i.e. precisely the code that took
the longest to get right against live hardware. Substituting the transport
underneath keeps one implementation of all of it.

The child is the same `~/scripts/robot_streaming_player.py` the laptop path
execs over SSH. One player on the robot, driven two ways.

### Capture: 48 kHz and gain

The robot's PortAudio exposes only raw `hw:3,0` — no plug layer, so no OS
resampler to lean on. The device is 48 kHz / S16_LE / mono only, and a 16 kHz
`InputStream` fails with `PaErrorCode -9997`. So:

- Capture at 48 kHz in `SILERO_FRAME_SIZE * 3` blocks.
- Per frame, `resample_poly(x, 1, 3)` down to 512 samples for the VAD. Plain
  `[::3]` would fold everything above 8 kHz into the speech band as noise —
  exactly where the VAD is looking.
- At end of turn, resample the **whole** recording in one pass for Gemini, so
  its ASR gets continuous filter output rather than stitched per-frame blocks.
- Multiply by `CAPTURE_GAIN_LINUX = 3.0` before anything sees the samples.

That last one matters more than it looks. Measured on the robot, speech peaks
at -15..-20 dBFS and the quietest real segments landed at 0.031-0.049 against
`MIN_PEAK_FLOOR = 0.03`. Silero separated speech perfectly at those levels
(p=1.000 speech, p<0.07 silence), but the energy gate's margin was thin enough
that a softer speaker would start getting turns rejected. Raising the capture
to laptop-equivalent levels is the right fix; lowering `MIN_PEAK_FLOOR` would
have widened the gate to room noise as well.

### VAD: same model, different runtime

`vad.py` needs torch, which the robot does not have and should not get (~1 GB
against ~3.7 GB free). `vad_onnx.py` is a numpy reimplementation of
`silero_vad.utils_vad.OnnxWrapper` — same weights, `onnxruntime` (already in
the robot's venv), no torch. It reproduces the model contract exactly,
including the 64-sample sliding context that gets prepended to each frame.

`tools/test_vad_parity.py` runs both over `archive/bench_input_he.wav` and
compares frame by frame. First run: max probability difference `0.000000`,
80/80 speech frames identical, zero decision disagreements. Re-run it if either
backend changes — every endpointing tunable was calibrated on the torch
numbers, so a drift there silently retunes the whole turn loop.

`vad.py`'s torch import is now inside `__init__` rather than at module scope,
because `conversation.py` imports `SileroVAD` unconditionally and an
import-time torch dependency makes the entire app unimportable on the robot.
`paramiko` is guarded the same way, for the same reason.

### The launcher

`tools/start_reachy.py`, run via `start.ps1` from the `reachy-mini` folder.
Discovers the robot by scanning the laptop's own /24 (so a new venue costs
nothing), probes for the K11 on both machines, and recommends a mode.

The mic probe is worth describing because it answers a better question than
"is a device present". A receiver whose transmitter is off enumerates
perfectly and returns **exactly** zero — digital silence, not a low level. A
linked receiver in a quiet room returns ~2e-4 RMS. So `peak > 1e-5` cleanly
separates "the link is up" from "the transmitter is off", which is invisible
to anyone looking at the hardware and has cost real debugging time before.

Single-instance enforcement is the other reason it exists: both modes drive
the same robot through the same SDK and the same player, and two at once fight
over the audio device. The symptom — robot moves but never speaks — is
indistinguishable from the broken-mic failure this project already works
around, so the launcher kills everything before starting anything.

## Editing the show script from the board

`show_editor.py` puts add / edit / delete behind the operator board, so a line
can be reworded the evening before a talk without a terminal or a JSON editor.

It deliberately reuses `tools/build_show.py`'s `synthesise()` and `text_hash()`
rather than reimplementing them, so a cue edited in the browser is byte-identical
to one built from the command line, and neither invalidates the other's cache.
`docs/SHOW_SCRIPT.md` is regenerated on every write — a change that updates the
robot but not the printed sheet the speaker is reading from is worse than no
change at all.

Guards, in rough order of how much trouble they save:

- **Motion names are validated against the installed catalogs** on save. An
  unknown name fails *silently* on the robot (it logs and keeps the current
  move), so without this a typo looks like a cue that simply doesn't move,
  mid-lecture, with no visible cause.
- **Backups.** Every write copies the previous `cues.json` into
  `show/backups/`, last 30 kept. It is the one artifact here with no other copy.
- **Atomic writes** (`.tmp` + `replace`) and a re-entrant lock, so two browser
  tabs cannot interleave a read-modify-write and silently lose a line.
- **Hotkey collisions** and cues that would do nothing (no text, no motion) are
  rejected with a message rather than saved.
- Deleting a cue that is currently playing returns 409 instead of pulling the
  WAV out from under the streaming thread.

Synthesis runs on a worker thread (`asyncio.to_thread`) because it is a
multi-second network call and a blocked event loop would freeze the board
mid-edit.

The card is a `<div>` with an inner fire target rather than a `<button>`,
because the edit control lives inside it and a button inside a button is
invalid HTML that browsers repair unpredictably. The edit button calls
`stopPropagation` — without it, clicking edit would also fire the cue and
announce the line to the room.

`tools/test_show_editor.py` exercises the whole round trip against the real
show directory, including one live TTS call, and asserts `cues.json` is
restored byte-for-byte afterwards.

## Silence handling — when Gemini doesn't answer

Rewritten 2026-07-27 after a live run where three turns in a row produced ~90 s
of a robot doing nothing. Applies to both modes.

### What actually happens

A single VAD frame over threshold — a tap, a chair, servos — starts a turn, and
the 0.8 s hangover guarantees the recording is ~0.9 s. That always clears
`MIN_SPEECH_S` (0.3 s), **because that check is on recording length, not speech
content**. So a second of room tone goes to Gemini as a question, and Gemini
correctly says nothing.

The old code read "no reply" as a wedged session: it waited 25 s, then closed
and reopened the session. Both halves were wrong.

### Why there is no VAD-side fix

The obvious answer is a minimum speech-content threshold. It was implemented,
measured against all 122 historical turns that got a reply, and **removed**.
A 0.3 s bar catches 14 genuine noise turns but also kills four real one-word
answers — "בסדר.", "לא.", "לא, נו.", "later out" — which register 1-4 speech
frames each. The two classes overlap completely on frame count, on `max_prob`
(real 0.56-0.80, noise 0.53-0.86) and on `mean_prob`. Whether a 1-frame turn
was speech is only knowable from what the ASR makes of it, i.e. after sending.

`record_with_vad` therefore logs speech content but does not gate on it. The
real cure is push-to-talk, which removes the guess rather than tuning it.

### So: make a no-reply cheap instead

`tools/exp_session_reopen.py` established against the live API that (a) silence
gets no reply, which is correct behaviour, and (b) a silent turn does **not**
poison the session — the next turn on the same session answers normally. So:

| Situation | Detection | Then |
|---|---|---|
| Nothing arrived at all | `_first_chunk_budget_s(mic_record_s)` + `DRAIN_NO_REPLY_ABORT_S` | Log, keep session, listen again |
| Audio started, then stopped | `DRAIN_WATCHDOG_TIMEOUT_S` + `DRAIN_HARD_ABORT_S` | Abort, close, reopen |

The second row is the genuine zombie case the reopen was written for, and it is
untouched — the experiment says nothing about it.

### The pre-first-chunk budget is per-turn

First-chunk latency tracks how much audio was sent: fitted across every logged
turn, `first_chunk ≈ 1.5 + 0.44 × recording`. A single fixed budget has to be
sized for the longest turn, which is why a 0.9 s junk turn used to sit in
silence for 13 s. `_first_chunk_budget_s` scales it:

```
budget = clamp(3.5 + 0.8 × recording, 4.0, 12.0)
```

Worst-case headroom over every observed first-chunk time is 1.61×, and a
noise-triggered turn now recovers in ~7 s rather than 13 s (originally 25 s).
`drain.timeout` records `threshold_s` and `mic_record_s` so the event stays
interpretable now that the budget varies per turn.

`DRAIN_WATCHDOG_TIMEOUT_S` was also raised 5.0 → 8.0: it fired on three
*successful* turns (5.5 / 5.9 / 5.7 s gaps before `turn_complete` on long
responses). A watchdog that cries wolf on healthy turns trains you to ignore it.

## Phase 3C — listening state machine, idle prompts, energy gate, adaptive VAD

Phase 3C is laptop-side only; no wire-protocol changes, no robot changes.
What it adds:

### Listening state machine

The old `while True: record_with_vad(); ...` loop is gone. The listen
loop is now a state machine:

| State | Idle prompt? | Recalibrate? | Ceiling? |
|---|---|---|---|
| `WAITING_FOR_FIRST_SPEECH` | no | no (startup-calibrated) | yes |
| `POST_ROBOT_LISTENING`     | after `IDLE_TRIGGER_S` (default 15 s) | yes (after 2 s silence, once per pass) | yes |

State transitions are driven by the new `listen_pass(...)` function.
`Turn N` is only logged when **N** is a real user turn — silent starts no
longer print "Turn 1" before anyone speaks. After the user's first
successful turn, `has_user_spoken_yet` flips true and subsequent passes
run in `POST_ROBOT_LISTENING` mode.

### Idle prompts (`idle_do_nothing` tool)

When the user has been silent for 15 s after the robot's response, the
laptop sends a small **text** message to Gemini on the same Live session:

> "[Idle update: 15s of silence since you last spoke. Pick a small
> expression — a brief emotion, a move_head glance, or idle_do_nothing
> to stay still. Do NOT speak. Respond with one function call only.]"

Gemini's response is drained with audio chunks **discarded** (defence-in-
depth in case the model speaks anyway). Only tool calls are dispatched.
A new fourth tool, `idle_do_nothing(reason?: string)`, gives Gemini an
explicit way to say "stillness is the right answer this time".

After the dispatched idle motion finishes (duration is looked up via
`_estimate_motion_duration` — emotions / dances / head-goto all known)
the 15 s clock restarts. If Gemini returns no tool call within ~5 s, the
clock just restarts and we go back to listening.

### Minimum-energy gate

Every recording VAD endpoints is gated post-hoc on peak amplitude:

```
peak < max(MIN_PEAK_FLOOR=0.03, 4 × current_noise_floor_rms) → rejected
```

Rejected recordings produce a log line and are *not* sent to Gemini,
*not* counted as a turn. They DO add their elapsed time to the
`cumulative_silence_s` counter (so the 5-minute hard ceiling still
fires even in a noisy room that keeps tripping VAD on ambient sounds).

### Adaptive ambient recalibration

While in `POST_ROBOT_LISTENING`, after 2 s of consecutive silence
within a pass, we take the most recent 0.3 s of frames, compute their
RMS, and use it as the new `noise_floor_rms`. Logged as
`vad: noise_floor recalibrated 0.0245 → 0.0410`. At most one
recalibration per listen pass.

### 5-minute hard ceiling

`SESSION_SILENCE_CEILING_S` (default 300 s, override via `CHAT_CEILING_S`
env var) is the maximum cumulative silence allowed. When reached:

- `[INFO] Session ceiling hit (5 min cumulative silence (300s)). Exiting.`
- Transcript receives a `[session ended: <reason>]` line.
- Robot SSH channel closes cleanly; rc=0.

### Configurable knobs

| Constant | Default | Env override | Effect |
|---|---:|---|---|
| `IDLE_TRIGGER_S` | 15.0 s | `CHAT_IDLE_TRIGGER_S` | Silence → idle prompt |
| `SESSION_SILENCE_CEILING_S` | 300.0 s | `CHAT_CEILING_S` | Silence → exit |
| `MIN_PEAK_FLOOR` | 0.03 | — | Absolute floor for the energy gate |
| `MIN_PEAK_NOISE_MULT` | 4.0 | — | Floor multiplier against `noise_floor_rms` |
| `RECAL_AFTER_SILENCE_S` | 2.0 s | — | Silence before adaptive recalibration |

### One-tool-per-turn system prompt nudge

The Hebrew system prompt grew one sentence: "בשיחה רגילה השתמש לכל היותר
בכלי אחד לתור — בחר את הביטוי המתאים ביותר במקום לשלב כמה כלים."
("In normal conversation use at most one tool per turn — pick the most
fitting expression rather than combining tools.") The idle prompt has
its own one-call instruction inline, so the system-prompt rule doesn't
apply to it.

### Quick test recipes

```powershell
# Test C: ceiling exit (no microphone interaction needed — anything noisy
# tripping VAD will still be rejected by the energy gate, and cumulative
# silence reaches the ceiling). Override the ceiling to 30 s for speed.
$env:CHAT_CEILING_S = "30"
python laptop_chat.py
Remove-Item Env:CHAT_CEILING_S
```

Look for the line:
```
[INFO] Session ceiling hit (5 min cumulative silence (31s)). Exiting.
```
and an empty conversation folder containing only `transcript.txt` with
the `[session ended: ...]` line.
