# Reachy Mini Dashboard — Phase A.5

Small focused phase between Phase A (functional but unstyled dashboard, complete and verified) and Phase B (styling, chat box polish, daemon heartbeat, substates). Three features.

## Goal

1. Add a **Stop System / Start System** pair of buttons next to the existing Start Conversation button. Stop System tears down the robot connection and the running robot player while keeping the web server and VAD model alive in memory. Start System brings the robot side back up. Lets the handler step away for hours without leaving Reachy breathing to an empty room, and without killing the dashboard.
2. Add a **Copy Logs** button on the event-log panel that copies the current ring buffer to the clipboard. Plain text, one line per entry, same format the UI displays.
3. (Carry-over from the Phase B backlog, ride-along) Show a `(turn aborted)` placeholder line in the chat box when a turn ends with `aborted=true` and no transcript landed. Optional in this phase; include only if it's a tiny change.

## State machine extension

Existing states: STARTING, IDLE_BREATHING, CONVERSATION_RUNNING, SHUTTING_DOWN.

Add: **STOPPED**.

```
                  ┌──────────────────┐
                  │     STARTING     │
                  └────────┬─────────┘
                           │ ready
                           ▼
       ┌─────────────────────────────────────┐
       │           IDLE_BREATHING            │ ◄──┐
       │           (robot breathing)         │    │ conversation
       └───┬────────────────────┬────────────┘    │ ended
           │                    │                  │
           │ Stop               │ Start Conv       │
           │                    ▼                  │
           │     ┌──────────────────────┐          │
           │     │ CONVERSATION_RUNNING │──────────┘
           │     └────────┬─────────────┘
           │              │ Stop (implicit end)
           │              │
           ▼              ▼
       ┌──────────────────────┐
       │       STOPPED        │
       │ (robot disconnected, │
       │  web server alive)   │
       └──────────┬───────────┘
                  │ Start System
                  ▼
              STARTING (again)

SHUTTING_DOWN remains process-exit only (Ctrl-C, OS signal). Not reachable from UI.
```

### Transition rules

| From               | Stop System | Start System | Start Conv | End Conv |
|--------------------|-------------|--------------|------------|----------|
| STARTING           | 409         | 409          | 503        | 409      |
| IDLE_BREATHING     | 200 → STOPPED | 409        | 200 → CONV | 409      |
| CONVERSATION_RUNNING | 200 → STOPPED (implicit end) | 409 | 409 | 200 → IDLE |
| STOPPED            | 409         | 200 → STARTING | 503     | 409      |
| SHUTTING_DOWN      | 409         | 409          | 409        | 409      |

"Implicit end" means: Stop while CONVERSATION_RUNNING gracefully signals the conversation to end (same path as the End Conversation button), awaits the turn loop to exit, then proceeds with the system teardown. No mid-turn audio gets cut; the user's current utterance, if any, runs to its natural turn boundary.

## Backend changes

### `SystemManager` additions

- **`async stop_system(reason: str = "user_stop")`**
  - Allowed from IDLE_BREATHING or CONVERSATION_RUNNING.
  - If state == CONVERSATION_RUNNING: await `end_conversation(reason="user_stop")`, which sets the conversation's stop_event and awaits its run() coroutine.
  - Then teardown: send EOF on the robot player's stdin → await the player process exit → close the SSH transport. Use `asyncio.to_thread` for the SSH close per the Phase A pattern.
  - Keep alive: VAD model, broadcaster, WS clients, system-level log, the FastAPI app.
  - Transition: → STOPPED. Broadcast `state.change`.

- **`async start_system(reason: str = "user_start")`**
  - Allowed only from STOPPED.
  - Transition: STOPPED → STARTING. Broadcast `state.change`.
  - Reuse the existing startup() machinery for the robot side: open new SSH, spawn new robot player, wait for "ready". Same `asyncio.to_thread` offload.
  - Reuse the already-loaded VAD model (do not reload).
  - Transition: STARTING → IDLE_BREATHING. Broadcast `state.change`.

- **Bookkeeping**: a `_pending_stop: bool` flag is the cleanest way to handle the "Stop while CONVERSATION_RUNNING" case. Set it true at the start of `stop_system()`, then in the `_run_conversation` finally block, check the flag: if set, transition to STOPPED rather than IDLE_BREATHING, and run the system-teardown there. Confirm during the audit whether a flag or a different control-flow approach (e.g. stop_system awaits end_conversation then runs teardown directly) is cleaner against the current code.

### REST endpoints

```
POST /api/system/stop
  → 200 { state: "STOPPED" }
  → 409 { error: "invalid_state", state }

POST /api/system/start
  → 200 { state: "IDLE_BREATHING" }  (after STARTING completes)
  → 409 { error: "invalid_state", state }
```

`/api/system/start` can either return when STARTING begins (immediate 200 with state STARTING) or block until IDLE_BREATHING. The latter is more pleasant for the UI button-feedback pattern. Robot ready takes ~7 seconds in practice; that's acceptable for a button hold-time. Pick the blocking variant.

### WebSocket events

No new event types. Existing `state.change` already broadcasts every transition; STOPPED, STARTING (again), and IDLE_BREATHING (again) all flow naturally through it. The `state.snapshot` on client connect must include `STOPPED` as a possible value.

### Process-level shutdown

Ctrl-C and OS signals must still run the full `shutdown()` path (which is process-exit), reachable from any state including STOPPED. If state is STOPPED at shutdown, just close uvicorn cleanly — no robot teardown needed. Make sure the existing signal handler doesn't crash on STOPPED.

## UI changes (still unstyled — Phase B will style)

### Buttons

The single primary button stays where it is (Start Conversation / End Conversation, driven by state).

Add a small **System Controls** row, separate from the conversation controls. Two buttons side by side:

```
[Stop System]   [Start System]
```

- Stop System enabled when state in {IDLE_BREATHING, CONVERSATION_RUNNING}, otherwise disabled.
- Start System enabled when state == STOPPED, otherwise disabled.
- Disabled = visibly grey + `disabled` attribute (real disabled, not just visually).

When state == STOPPED:
- The primary Start Conversation button is also disabled (state column already handles this via the existing button-state logic, just confirm STOPPED maps to disabled there).

### Copy Logs button

A small button labeled "Copy" or "Copy logs" in the top-right corner of the event-log panel. On click:

- Read every line currently in the broadcaster ring buffer (the same source the panel renders from on the client side).
- Format each line as `HH:MM:SS.mmm  LEVEL  logger  message` matching the UI display.
- Join with `\n`.
- `await navigator.clipboard.writeText(text)`.
- Show a brief inline confirmation ("Copied", 1500 ms) then revert.
- On error (clipboard API unavailable, permission denied): show "Copy failed" inline, no exception thrown.

The clipboard API works on `http://127.0.0.1` in all modern browsers without requiring HTTPS, so this just works. No backend changes.

### Aborted-turn placeholder (optional)

In the chat box, when a `conversation.ended` event arrives with `reason: "gemini_silent"` OR a `turn.end` arrives with `aborted: true` and no preceding `transcript.user` or `transcript.robot` for that turn_id, render a centered grey line: `(turn aborted)`. If this turns out to need non-trivial state tracking on the client, drop it and we'll do it properly in Phase B.

## Step 0 — snapshot

Same pattern as Phase A. Snapshot the live tree to `archive/snapshot_pre_phase_a5/` with a `MANIFEST.md` containing file count, line count, and rollback commands. Exclude `archive/`, `conversations/`, `.venv/`, `__pycache__/`. Verify integrity before changing any code.

## Definition of done

Verifiable without a long conversation:

1. `Stop System` while IDLE_BREATHING: state goes STOPPED in ~2 seconds, robot stops breathing (motion loop exits), SSH closed, robot player process exited rc=0, web page still reachable, WS still connected.
2. `Start System` while STOPPED: state goes STARTING → IDLE_BREATHING in ~7-10 seconds, robot resumes breathing, fresh SSH and robot player.
3. Stop → Start → Stop → Start round trips work twice in a row with no leaked processes (check `tasklist | findstr python` and that no orphan paramiko threads remain).
4. `Stop System` while CONVERSATION_RUNNING: turn loop ends gracefully (no mid-frame audio cut), then state goes STOPPED. The pending conversation dir gets a normal `summary.json` and `transcript.txt` like any user-ended conversation.
5. `Start Conversation` button is disabled while state == STOPPED, and 503 if called via REST anyway.
6. Ctrl-C at any state cleanly shuts down the process. From STOPPED, this just exits uvicorn (no robot teardown attempted, no exception raised).
7. Copy Logs button: clicking it copies the visible log lines to the clipboard as plain text in the documented format. Confirm by pasting into a text editor.

Requires one conversation:

8. After Stop System, no further turn loop activity. No mic capture. No phantom Gemini sessions.

## Open questions to confirm before starting

1. **Pending-stop flag vs alternative**: audit the current `_run_conversation` and confirm whether a `_pending_stop` flag is the cleanest way to route the post-conversation transition to STOPPED instead of IDLE_BREATHING, or whether a different control-flow shape fits the existing code better.
2. **/api/system/start blocking vs not**: confirm blocking (returns 200 once IDLE_BREATHING is reached, ~7-10 s). If anything in the current uvicorn config has a short request timeout that conflicts, surface it.
3. **Aborted-turn placeholder scope**: include in Phase A.5 if it's a small client-side change, or defer to Phase B if it needs new event types or non-trivial state tracking. Your call after auditing the existing chat-render logic.

## Out of scope (defer to Phase B)

- Tailwind styling.
- Two-column layout.
- Color-coded log levels.
- Robot daemon heartbeat probe.
- Turn substates (LISTENING / THINKING / SPEAKING).
- Uptime counter.
- Error-state UI with Reset button.

## Workflow expectations

Same as Phase A: audit, propose adjustments, sign-off, implement, report. Stop and wait for "go" before writing code. Don't bundle Phase B work into this.
