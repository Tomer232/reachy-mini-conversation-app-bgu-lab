# Reachy Mini Handler Dashboard — Plan

## Goal

Replace the current CLI-only `laptop_chat.py` workflow with a web dashboard:

1. System startup loads VAD, connects to the robot, and the robot enters breathing idle, without starting a conversation.
2. The system serves a local web UI at `http://127.0.0.1:8765` (default port; configurable).
3. The handler clicks "Start Conversation" in the UI to begin a Gemini Live session and the turn loop. "End Conversation" tears the session down cleanly; the robot stays connected and keeps breathing.
4. The UI shows live system state, robot connection status, an event-log tail, and a chat-style transcript stream during conversations.

Future users are non-technical, so the UI must be a real GUI, not a terminal.

## Stack

- FastAPI + `uvicorn[standard]` for the HTTP server and WebSockets. Add to `.venv` if not present.
- HTML + vanilla JS + Tailwind via CDN. No build step. No npm.
- Browser auto-launched via Python's `webbrowser.open` after the server is listening.
- One Python process. No new venv.

## Current architecture (reference)

`laptop_chat.py` is a monolithic async `main()`:

1. Load Silero VAD.
2. SSH connect, start `robot_streaming_player.py`, wait for `ready` handshake.
3. Open Gemini Live session.
4. `while True` turn loop:
   - Capture mic, run VAD until speech end.
   - Send audio batch to Gemini.
   - Drain audio chunks and tool_calls, forward over SSH stdin to the robot player.
   - Append to transcript, check end phrases, write per-turn WAV.
5. On exit: close session, close SSH, generate summary.

There is no separation today between "system alive" and "conversation running".

## Target architecture

### State machine

```
                  ┌─────────────────────────┐
   laptop_chat.py │      STARTING           │   initial
        ───────►  │ (loading VAD, SSH, …)   │
                  └────────────┬────────────┘
                               │  on ready
                               ▼
                  ┌─────────────────────────┐
                  │    IDLE_BREATHING       │   robot connected,
   ┌──────────────┤  no Gemini session,     │   breathing motion live
   │              │  no mic, button: START  │
   │              └────────────┬────────────┘
   │                           │  POST /api/conversation/start
   │                           ▼
   │              ┌─────────────────────────┐
   │              │ CONVERSATION_RUNNING    │   Gemini open,
   │              │  substate: LISTENING /  │   turn loop active
   │              │  THINKING / SPEAKING    │
   │              │  button: END            │
   │              └────────────┬────────────┘
   │                           │  POST /api/conversation/end
   │                           │  OR end_phrase detected
   │                           │  OR conversation crash
   │                           ▼
   │              ┌─────────────────────────┐
   └───── back to │ IDLE_BREATHING (again)  │
                  └─────────────────────────┘

ERROR (transient): broadcast error, attempt recovery, back to IDLE_BREATHING.
ERROR (fatal):     broadcast error, no recovery. UI shows reason. State = STOPPED.

SHUTTING_DOWN: triggered by Ctrl-C in terminal or explicit shutdown call.
```

### Module breakdown

Keep what works. Refactor the monolith:

```
reachy_chat/
├── laptop_chat.py              NEW entry point: launch web server + system
├── system.py                   NEW SystemManager (state machine, owned resources)
├── conversation.py             NEW Conversation (turn loop, extracted from laptop_chat)
├── web/
│   ├── __init__.py
│   ├── server.py               FastAPI app, routes, WebSocket
│   ├── broadcaster.py          WS broadcast helper (one-to-many)
│   └── static/
│       ├── index.html
│       ├── app.js
│       └── styles.css          minimal; rest via Tailwind CDN
├── vad.py                      unchanged
├── logging_setup.py            extend for system-level log
├── event_log.py                unchanged
├── summary.py                  unchanged
├── robot_speech_tapper.py      unchanged
└── robot_streaming_player.py   unchanged
```

### Class responsibilities

**`SystemManager`** (singleton, owned by FastAPI lifespan)

Holds for the system's lifetime:

- Silero VAD model (loaded once, reused across conversations via `vad.reset()`).
- SSH transport and the running `robot_streaming_player.py` process.
- System-level logging.
- State: a `SystemState` enum value.
- Current `Conversation` instance or `None`.
- A `Broadcaster` for WS fan-out.

Methods:

- `async startup()` — STARTING → IDLE_BREATHING. Loads VAD, opens SSH, spawns the robot player, waits for `ready`. Broadcasts each transition.
- `async start_conversation() -> Conversation` — requires `state == IDLE_BREATHING`. Creates a fresh `conversations/<timestamp>/` dir, instantiates `Conversation`, schedules its `run()` as a background task, transitions to CONVERSATION_RUNNING.
- `async end_conversation(reason: str)` — requires `state == CONVERSATION_RUNNING`. Signals the conversation to stop via its `stop_event`, awaits cleanup, transitions back to IDLE_BREATHING.
- `async shutdown()` — stops conversation if running, closes SSH (sends EOF on stdin so the robot player exits cleanly), transitions to SHUTTING_DOWN. Idempotent.

**`Conversation`** (ephemeral, one per started conversation)

Owns:

- Gemini Live session.
- Per-conversation directory and its `EventLogger`.
- Per-turn WAV writers, transcript file, `timings.csv`.

Methods:

- `async run(stop_event: asyncio.Event)` — the turn loop. Exits when `stop_event` is set, end-phrase detected, or fatal error. All current behaviors must be preserved: VAD, drain watchdog (`DRAIN_FIRST_CHUNK_TIMEOUT_S`, `DRAIN_WATCHDOG_TIMEOUT_S`, `DRAIN_HARD_ABORT_S`), `MAX_TOOL_CALLS_PER_TURN`, end-phrase list, `events.jsonl`, summary generation.
- `async stop(reason: str)` — sets the stop event, awaits run() completion, runs cleanup (close Gemini session, finalize summary).

`Conversation.run()` is where most of today's `main()` body lives, lightly adapted to:

- Take callbacks injected by `SystemManager` (`on_turn_state`, `on_transcript`, `on_log`, `on_end`) instead of writing directly to the console.
- Honor an external `stop_event` alongside the existing end-phrase check.
- Reuse the pre-loaded VAD model from `SystemManager` instead of loading its own.

### REST API

```
GET  /api/status
  → { state, robot: {connected, ip, daemon_status},
      conversation: null | {id, started_at, turn_count} }

POST /api/conversation/start
  → 200 { conversation_id, dir }     if state == IDLE_BREATHING
  → 409 { error: "already_running" } if state == CONVERSATION_RUNNING
  → 503 { error: "not_ready", state } otherwise

POST /api/conversation/end
  → 200 { summary }                  if state == CONVERSATION_RUNNING (best-effort summary)
  → 409 { error: "not_running" }     otherwise
```

### WebSocket protocol

URL: `/ws`.

On client connect, server immediately sends a snapshot:

```json
{"event": "state.snapshot",
 "state": "IDLE_BREATHING",
 "robot": {"connected": true, "ip": "10.100.102.18", "daemon_status": "active"},
 "conversation": null,
 "recent_log": ["...", "...", "..."]}
```

Server pushes during operation:

```json
{"event":"state.change","state":"CONVERSATION_RUNNING"}
{"event":"robot.status","connected":true,"ip":"...","daemon_status":"active"}
{"event":"conversation.started","id":"2026-05-27_10-54-44","dir":"..."}
{"event":"conversation.ended","reason":"user|end_phrase|crash","summary":{...}}
{"event":"turn.state","state":"LISTENING|THINKING|SPEAKING","turn_id":3}
{"event":"transcript.user","turn_id":3,"text":"היי, מה שלומך?"}
{"event":"transcript.robot","turn_id":3,"text":"שלומי טוב, איך אתה?"}
{"event":"log","level":"INFO","logger":"reachy.gemini.session","msg":"...","ts":"10:55:01.234"}
{"event":"error","where":"ssh|gemini|player","message":"..."}
```

Substate mapping (from existing events):

- LISTENING: between `user.speech.start` and `user.speech.end`.
- THINKING: between `user.speech.end` and `gemini.recv.first_chunk`.
- SPEAKING: between `gemini.recv.first_chunk` and `turn.end`.
- (Between turns: LISTENING resumes immediately as the next mic capture begins.)

Multiple WS clients may connect; all receive the same broadcast stream. Any client can issue start/end via REST. No auth (localhost only).

### UI layout

Two-column, browser fills the viewport. The page itself does not scroll; panels scroll internally.

```
┌─────────────────────────────────────────────────────────────────────┐
│  Reachy Mini Handler Dashboard                  [State: IDLE]       │
├────────────────────────────────┬────────────────────────────────────┤
│ LEFT COLUMN (~40%)             │ RIGHT COLUMN (~60%)                │
│                                │                                    │
│ ┌─ System state ─────────────┐ │ ┌─ Conversation ─────────────────┐ │
│ │ ● IDLE_BREATHING           │ │ │  (empty while idle)            │ │
│ │ Substate: —                │ │ │                                │ │
│ │ Uptime: 00:12:34           │ │ │  YOU:   …                      │ │
│ └────────────────────────────┘ │ │  ROBOT: …                      │ │
│                                │ │  YOU:   …                      │ │
│ ┌─ Robot connection ─────────┐ │ │  ROBOT: …                      │ │
│ │ ● Connected                │ │ │                                │ │
│ │ IP: 10.100.102.18          │ │ │                                │ │
│ │ Daemon: active             │ │ │                                │ │
│ └────────────────────────────┘ │ │                                │ │
│                                │ └────────────────────────────────┘ │
│ ┌─ Event log ────────────────┐ │                                    │
│ │ 10:54:44 INFO  VAD ready   │ │ ┌─ Controls ─────────────────────┐ │
│ │ 10:54:53 INFO  robot ready │ │ │                                │ │
│ │ 10:55:01 INFO  turn 1      │ │ │   [  START CONVERSATION  ]     │ │
│ │ 10:55:06 INFO  turn 2      │ │ │                                │ │
│ │ … (autoscroll)             │ │ │   (END button replaces it      │ │
│ │                            │ │ │    while running)              │ │
│ └────────────────────────────┘ │ └────────────────────────────────┘ │
└────────────────────────────────┴────────────────────────────────────┘
```

State indicator dot:

- Gray: STARTING / SHUTTING_DOWN
- Green: IDLE_BREATHING
- Blue: CONVERSATION_RUNNING (with substate badge nearby)
- Red: ERROR / STOPPED

Chat box:

- Cleared at the start of each new conversation; empty while idle.
- YOU right-aligned, ROBOT left-aligned, classic chat-app feel.
- Hebrew RTL handled via `dir="auto"` on each message.
- Autoscroll to bottom unless the user has scrolled up.
- Show turn number and a small ms timestamp on hover.

Event log:

- Last 200 lines.
- Color by level: INFO neutral, WARNING amber, ERROR red.
- Autoscroll unless user has scrolled up.

Controls:

- A single primary button at all times:
  - IDLE_BREATHING: "Start Conversation" (enabled).
  - STARTING / SHUTTING_DOWN: disabled, label "…".
  - CONVERSATION_RUNNING: "End Conversation" (red).
  - ERROR: "Reset" (attempts recovery).

## Phasing

Three phases. Each ends with a demoable build. Do not start Phase B until Phase A is signed off.

### Phase A: backend skeleton + state machine

Goal: web server runs, system reaches IDLE_BREATHING, button starts a real conversation that runs to completion (end phrase or button), robot breathes during idle. No styling.

Tasks:

1. Add `fastapi` and `uvicorn[standard]` to `.venv` if not present.
2. Create `system.py` with `SystemManager` and the state machine.
3. Extract the turn loop into `conversation.py` as `Conversation.run(stop_event)`. Preserve every existing behavior: VAD, drain watchdog, hard_abort, `MAX_TOOL_CALLS_PER_TURN`, end-phrase detection, summary generation, per-turn WAVs, `events.jsonl`, `timings.csv`, `transcript.txt`.
4. Create `web/server.py` with the FastAPI app, REST endpoints, `/ws`, and static file serving.
5. Create `web/static/index.html` and `app.js` at minimum: state text, robot status text, start/end button, `<pre>` for event log, `<div>` list for chat. No styling beyond browser defaults.
6. New `laptop_chat.py` entry point: parse args (port, etc.), construct `SystemManager`, start `uvicorn` programmatically, `webbrowser.open` the dashboard URL after the server is up.
7. Robot stderr forwarder: route to the system log when idle, to the conversation log when in conversation. Mirroring to both during a conversation is acceptable if it simplifies the code.

Definition of done for Phase A:

- `python laptop_chat.py` brings up the dashboard at `http://127.0.0.1:8765` and opens the browser automatically.
- The page shows IDLE_BREATHING and the robot IP within ~10 seconds of startup.
- The physical robot is visibly breathing while idle.
- Click Start: a conversation begins; transcript text appears as plain list items; end-phrase or End-button returns the system to IDLE_BREATHING.
- Click Start a second time: another conversation works; a new conversation dir is created.
- Ctrl-C in the terminal: clean shutdown of robot player, SSH, uvicorn.
- No regression vs the existing CLI: per-conversation `events.jsonl`, `laptop.log`, `summary.json`, `turn_NNN.wav`, `transcript.txt`, `timings.csv` are produced identically.

### Phase B: the actual dashboard

Goal: the layout above, styled with Tailwind, real chat box, real event-log panel.

Tasks:

1. Tailwind via CDN in `index.html`.
2. Two-column layout, sticky controls bar.
3. Chat box with RTL support, slide-in animation on new messages, autoscroll-on-bottom.
4. Event-log panel with level-color coding and autoscroll-on-bottom.
5. Robot status panel: connection dot, IP, daemon status. Refreshed at least every 5 s via an internal heartbeat ping (a no-op REST call to the daemon's `/api/daemon/status`).
6. System state panel: colored dot, substate badge, uptime counter.
7. Turn substate emitted by `Conversation` from existing event transitions.
8. Disabled-button states during STARTING / SHUTTING_DOWN.

Definition of done for Phase B:

- Layout matches the diagram at default zoom on a 1280-wide window.
- Chat box renders YOU and ROBOT messages with timestamps and correct alignment.
- Event log streams the last 200 lines and autoscrolls.
- Robot status flips to red within ~10 s of a dropped connection.
- Turn substate is visible during a conversation.

### Phase C: polish (backlog)

Treat as optional. Do not start unless Tomer asks.

1. Error states: SSH drop, Gemini failure, robot-player crash. Each shows a banner with a Reset button.
2. Connection retry: if SSH drops, attempt 3 reconnects with backoff before going to ERROR.
3. Settings panel (read-only first cut): show the active config flags from `laptop_chat.py` constants.
4. Recent conversations list: links to `conversations/<timestamp>/` dirs.

## Lifecycle gotchas

These will bite if not handled carefully:

- **Robot player shutdown order**: on `shutdown()`, stop the conversation first (set its stop_event and await), then close the SSH channel by sending EOF on stdin so the long-running player reads `b""` and exits its read loop. The current code already handles this cleanly in `laptop_chat.py`'s `finally` block; preserve the order.
- **Idempotency**: `start_conversation` and `end_conversation` should both be idempotent against rapid double-clicks. The state-machine check (`if self.state != IDLE_BREATHING: raise`) handles this naturally; just be sure to return 409, not crash.
- **Conversation crash inside `run()`**: the task should still transition the system back to IDLE_BREATHING, not leave it stuck in CONVERSATION_RUNNING. Wrap the conversation task with a callback that broadcasts the crash and triggers `end_conversation(reason="crash")`.
- **Gemini session teardown**: per the chosen end semantics, end always closes the Gemini session. No "warm" reuse. Confirms the behavior we already see post-hard_abort (close + reopen on next start in ~400 ms).
- **WebSocket reconnect**: a refreshed browser tab must work without re-starting the system. The `state.snapshot` on connect handles this; make sure the snapshot includes the last 200 log lines, the current state, robot status, and (if in a conversation) the transcript so far.
- **VAD model reuse**: load it once in `SystemManager.startup()` and pass a reference into each `Conversation`. Call `vad.reset()` at the start of each turn (existing behavior).

## Constraints and non-goals

- Localhost only. Bind to `127.0.0.1`. No auth in v1.
- Single user assumed; multiple browser tabs allowed but only one logical handler.
- Desktop browser only. No mobile layout target.
- No persistent settings storage in v1; config lives in module-level constants.
- Do not modify `robot_streaming_player.py`. Its wire protocol and motion behavior stay as-is.
- Do not modify `vad.py`, `event_log.py`, `summary.py`, `robot_speech_tapper.py`.
- The existing per-conversation output layout (`events.jsonl`, `laptop.log`, `summary.json`, `turn_NNN.wav`, `transcript.txt`, `timings.csv`) is preserved exactly. Downstream tooling depends on it.
- The known open issues (end-phrase fuzzy match, drain.hard_abort doc wording) are not in scope.

## Open questions to confirm before starting

Address these in your audit reply:

1. **Port**: any preference, or default `8765`?
2. **Config location**: `MAX_TOOL_CALLS_PER_TURN`, `DRAIN_*` timeouts, etc. currently in `laptop_chat.py` as module-level constants. Move to a `config.py`, or leave them where they live and import into `conversation.py`? Recommendation: leave in place during Phase A; revisit in Phase B if it becomes awkward.
3. **`--no-web` CLI fallback**: keep a way to run the old linear flow for debugging? Recommendation: no. If a regression appears, debug the new path directly. Confirm.
4. **Idle-time logs**: during pure idle (no conversation), where do system logs go? Recommendation: `system_logs/<startup_date>.log`, rotated per-startup. Confirm.

## Workflow expectations

- Phased: audit, propose adjustments, sign-off, implement, report.
- Pause for confirmation at phase boundaries.
- Reply with the audit and answers to the four open questions before writing any code. Stop and wait for "go" before starting Phase A.
- After each phase, report what was done, what wasn't, and anything surprising.
- Plain text. No emojis. No decorative headers in console output.
- If anything in this plan misjudges what's actually in the code today, stop and surface it instead of working around it silently.
