# Reachy Mini Dashboard — Phase B

The big visual phase. Phase A delivered functionality; Phase A.5 added Stop/Start System and Copy Logs; the log filter quieted the DEBUG flood. The system works. This phase makes it look like a real dashboard a non-technical handler can use, and adds the one missing functional piece: turn substates.

## Reference design

Tomer provided a mockup image. Describing it textually for the implementation:

- **Title bar** across the top: "Reachy Mini Handler Dashboard".
- **Two-column layout** below it. Left column ~55-60%, right column ~40-45%. The page fills the viewport at minimum 1280px wide; panels scroll internally, the page itself does not.
- **Left column**, top to bottom: a row of small status cards (3 across), then a wider Session Monitor card, then a tall Event Log card filling the remaining height.
- **Right column**: a tall Live Conversation Stream card filling the full available height.

Visual style throughout: card-based, white/very-light card backgrounds on a slightly tinted page background, subtle shadow, rounded corners (~8px radius), generous interior padding. Section titles small caps or bold sans-serif. The mockup feels clean and clinical, not playful — appropriate for a lab tool.

Use Tailwind via CDN. No build step. No React. Vanilla JS as in Phase A.

## Cards in detail

### Left column

#### 1. System Status (top row, leftmost)

Shows the current `SystemState` translated to a friendly label, with a colored dot:

| State              | Label              | Dot color |
|--------------------|--------------------|-----------|
| STARTING           | Starting…          | amber     |
| IDLE_BREATHING     | Idle               | green     |
| CONVERSATION_RUNNING | In Conversation  | blue      |
| STOPPED            | Stopped            | gray      |
| SHUTTING_DOWN      | Shutting Down      | amber     |
| ERROR              | Error              | red       |

A small substate badge appears below the main label when state is CONVERSATION_RUNNING: `LISTENING` / `THINKING` / `SPEAKING`, derived per the substate mapping below.

#### 2. Robot Connection (top row, middle)

Shows:

- Colored dot + "Connected" / "Disconnected" label.
- "IP: 10.100.102.18" line.
- "Daemon: active" line, driven by the heartbeat probe described in the backend section.

Connection comes from the SSH channel state already exposed in `state.snapshot.robot.connected` plus a periodic heartbeat to the robot's daemon (`http://<robot_ip>:8000/api/daemon/status`). Flip the dot red within ~10 s of a missed heartbeat.

#### 3. System Controls (top row, rightmost)

Two buttons in this card (the mockup shows three; "Configure" is a placeholder for Phase C — render it disabled with tooltip "Coming soon"):

- **Start System**: enabled when state == STOPPED.
- **Stop System**: enabled when state in {IDLE_BREATHING, CONVERSATION_RUNNING}.
- *(Configure: disabled placeholder)*

Wired to the existing `POST /api/system/start` and `POST /api/system/stop` from Phase A.5. No new endpoints.

#### 4. Session Monitor (second row, full width of left column)

Shows when state == CONVERSATION_RUNNING. Empty/muted when idle.

- Active Session ID: the `conversation_id` (the timestamp string like `2026-05-27_13-52-09`).
- Turn Count: total turns so far in the active conversation.
- Current Turn Duration: live counter for the current turn (mm:ss), starts at the LISTENING substate of each turn, freezes at turn end.
- A start/end conversation button at the bottom of this card:
  - **Start Conversation** when state == IDLE_BREATHING.
  - **End Conversation** when state == CONVERSATION_RUNNING.
  - Disabled in other states.

This relocates the primary conversation button from Phase A's standalone position into the Session Monitor card per the mockup layout.

#### 5. Event Log (third row, fills remaining left column height)

Color-coded lines, autoscroll-to-bottom unless the user has scrolled up:

- INFO: neutral foreground (gray-700 on white).
- WARNING: amber.
- ERROR: red.

Format unchanged from Phase A.5: `HH:MM:SS.mmm  LEVEL  logger  message`. The Copy Logs button stays in the card header, right-aligned, same behavior as Phase A.5.

The log filter from the recent fix means `player.frame_recv` lines no longer reach this panel. No additional filtering needed.

### Right column

#### 6. Live Conversation Stream

Chat-style transcript filling the full right column. Bubbles:

- **YOU** (the human): right-aligned, light gray bubble, RTL-aware (`dir="auto"` on each bubble).
- **Reachy Mini** (the robot): left-aligned, warm peach/orange bubble, small robot avatar to the left of the bubble.
- Below each bubble, a small muted metadata line showing `Turn: N`. Conversation ID shown once at the top of the card, not under every message (keeps long conversations tight).
- New message animation: slide in from bottom with a 150ms ease-out.
- Autoscroll to bottom unless the user has scrolled up.
- Cleared at `conversation.started`; persists until the next conversation starts.

For aborted turns (`turn.end` with `aborted=true` and no transcript for that turn), render a centered muted line in place of the missing transcript pair: `Turn N aborted — Gemini silent`. This is the carried-over item from Phase A.5.

## Backend changes

Most of the Phase B work is frontend. The backend additions:

### Substate emission

`Conversation` already passes through the existing per-turn `state.transition` events (IDLE / LISTENING / CAPTURING / SENDING / RECEIVING). Map those to the UI substate via a new `turn.substate` WS event emitted by the broadcaster on each transition:

| Existing _STATE transition | UI substate |
|----------------------------|-------------|
| `LISTENING` (mic capture)  | LISTENING   |
| `CAPTURING` (sending audio batch to Gemini, awaiting first chunk) | THINKING |
| `RECEIVING` (audio chunks flowing) | SPEAKING |

The mapping is straightforward: tap into the existing `_transition()` calls in `conversation.py` and broadcast `{event: "turn.substate", state: "LISTENING|THINKING|SPEAKING", turn_id: N}` whenever the per-turn state changes. No change to `events.jsonl` semantics.

### Robot daemon heartbeat

A background task in `SystemManager` that:

- Every 5 seconds while state in {IDLE_BREATHING, CONVERSATION_RUNNING}, performs a fast HTTP GET to `http://<robot_ip>:8000/api/daemon/status` with a 2 s timeout.
- On 200 response: broadcast `{event: "robot.heartbeat", ok: true, daemon_status: "active"}`.
- On timeout or non-200: broadcast `{event: "robot.heartbeat", ok: false}`. If two consecutive misses, broadcast `{event: "robot.status", connected: false, ...}` so the Robot Connection card flips to red.
- Disabled (no probe) while STOPPED or STARTING.

This is the first time we actually probe the daemon for liveness; previously we trusted the SSH channel and the initial `ready` handshake.

### Turn timing for Session Monitor

The Session Monitor card needs the current turn's elapsed duration as a live counter. Approach:

The backend already emits enough information (`turn.substate` transitioning to LISTENING marks turn start; the `turn.end` log event marks the boundary). Frontend ticks a local timer on a `setInterval`, resets it on each new LISTENING substate, and freezes it when state goes back to IDLE_BREATHING. No new backend event needed beyond the substate one above.

## Stack and conventions

- **Tailwind via CDN**: `<script src="https://cdn.tailwindcss.com"></script>` in `index.html`. No JIT customization; use core utilities.
- **Icons**: prefer inline SVG for the single robot icon, no extra dependency. Lucide via CDN is acceptable if multiple icons end up needed.
- **Fonts**: system stack via Tailwind defaults. Don't pull a Google Font.
- **No new JS frameworks**. Vanilla JS, same module structure as Phase A: `app.js`, possibly split into helpers (`chat.js`, `events.js`) if it grows past ~400 lines.

## Definition of done

One short conversation runs end-to-end through the new UI. Specifically:

- System Status card transitions through Starting → Idle → In Conversation → Idle, with dot colors matching the table.
- Substate badge appears under the main status during CONVERSATION_RUNNING and cycles LISTENING → THINKING → SPEAKING → LISTENING per turn, in sync with the audible behavior of the system.
- Robot Connection card shows green dot and IP during normal operation. Killing the SSH connection from the robot side (or hitting Stop System) flips the card red within ~10 s.
- System Controls and the Session Monitor's conversation button enable/disable correctly per the state matrix.
- Session Monitor populates with conversation ID, increments Turn Count per turn, and the duration counter ticks every second and resets on each new turn.
- Event Log shows color-coded lines, autoscrolls, Copy Logs still works.
- Chat box renders YOU right-aligned in gray, Reachy Mini left-aligned in peach with avatar. RTL renders correctly for Hebrew. Turn N metadata under each bubble. Conversation ID shown once at top.
- A hard_abort triggered turn results in a centered `Turn N aborted — Gemini silent` line in the chat box, with no orphan YOU bubble left dangling.
- Layout matches the mockup's general structure at 1280-wide and wider.

## Out of scope

Deferred to Phase C:

- Configure button (placeholder only in Phase B).
- Error state UI with Reset button (the ERROR state exists in the enum but no UI flow yet).
- Settings panel (read-only config flag display).
- Recent conversations list.
- Mobile / responsive layout.
- Sound effects, voice level meters, or other "showy" additions.
- Engineer observation notes (dropped from scope).

## Snapshot

Step 0, same pattern: `archive/snapshot_pre_phase_b/` with MANIFEST.md, exclude `archive/`, `conversations/`, `.venv/`, `__pycache__/`, integrity counts verified.

## Open questions to confirm before starting

1. **Configure button**: confirm it's a Phase C placeholder (disabled, tooltip "Coming soon") in Phase B, not a missing feature. Or if you have something specific in mind for it, name it now.
2. **Conversation ID label per message**: the mockup shows `Conversation ID: 123456 Turn: 4` under every message. Recommendation: show the conversation ID once at the top of the chat card and only `Turn: N` under each message. Confirm acceptable.
3. **Robot avatar**: confirm inline SVG (no extra dep) vs. Lucide-via-CDN. Recommendation: inline SVG for the single robot icon.
4. **Substate mapping**: confirm the LISTENING / CAPTURING / RECEIVING → LISTENING / THINKING / SPEAKING mapping is what you want. The CAPTURING state in current code is "audio captured, sending to Gemini" — calling that THINKING reads correctly to a non-technical user.

## Workflow expectations

Same as previous phases: audit, propose adjustments, sign-off, implement, report. Plain text. No emojis. No decorative headers in console output. Don't bundle Phase C work in.
