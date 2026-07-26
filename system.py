#!/usr/bin/env python3
"""SystemManager: the singleton that owns the system's long-lived resources
and the system-level state machine.

Lifetime-owned resources (loaded once, reused across conversations):
  - the Silero VAD model,
  - the SSH transport + the running robot_streaming_player.py process,
  - the genai client,
  - a Broadcaster for WebSocket fan-out.

System states (distinct from the per-turn fsm in conversation.py):

  STARTING            loading VAD, opening SSH, waiting for the robot
  IDLE_BREATHING      robot connected and breathing, no Gemini session
  CONVERSATION_RUNNING a Conversation.run() task is active
  ERROR               recoverable/non-recoverable failure surfaced to the UI
  STOPPED             fatal; no recovery
  SHUTTING_DOWN       teardown in progress

The blocking calls (VAD load, SSH connect, robot.close) are run off the event
loop via asyncio.to_thread so the web server stays responsive during STARTING
and SHUTTING_DOWN (Phase A adjustment B).
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time

import httpx
from google import genai

import conversation as conv_mod
from conversation import (
    Conversation,
    ConversationDir,
    StreamingRobotPlayer,
    SileroVAD,
    get_api_key,
    get_robot_host,
    ROBOT_USER,
    ROBOT_PASSWORD,
    GEMINI_MODEL,
    GEMINI_INPUT_RATE,
    SILERO_THRESHOLD,
    SILERO_FRAME_SIZE,
)
from logging_setup import open_conversation_log, close_conversation_log


log = logging.getLogger("reachy.system")

# Phase B robot daemon heartbeat. The daemon serves an HTTP status endpoint on
# port 8000, independent of the SSH transport (port 22) — probing it never
# touches the paramiko channel. Cadence 5 s, timeout 2 s, only while the robot
# is meant to be up (IDLE_BREATHING / CONVERSATION_RUNNING). The full URL is
# built per-instance from the resolved robot host (see SystemManager.__init__).
HEARTBEAT_INTERVAL_S = 5.0
HEARTBEAT_TIMEOUT_S = 2.0
HEARTBEAT_MISS_LIMIT = 2  # consecutive misses before flipping the card red


class SystemState(enum.Enum):
    STARTING = "STARTING"
    IDLE_BREATHING = "IDLE_BREATHING"
    CONVERSATION_RUNNING = "CONVERSATION_RUNNING"
    ERROR = "ERROR"
    STOPPED = "STOPPED"
    SHUTTING_DOWN = "SHUTTING_DOWN"


# REST-mapped control errors. The web layer translates these to HTTP codes.
class ConversationConflict(Exception):
    """A start/end request that conflicts with the current state (HTTP 409)."""


class NotReady(Exception):
    """A start request while the system isn't IDLE_BREATHING (HTTP 503)."""
    def __init__(self, state: str):
        super().__init__("not_ready")
        self.state = state


class InvalidSystemState(Exception):
    """A system stop/start request that conflicts with the current state
    (HTTP 409, body {error: "invalid_state", state})."""
    def __init__(self, state: str):
        super().__init__("invalid_state")
        self.state = state


class SystemManager:
    def __init__(self, broadcaster, robot_host: "str | None" = None):
        self.broadcaster = broadcaster
        # Robot SSH host, resolved once here at startup: --robot-host arg >
        # REACHY_ROBOT_HOST env > default. Single source of truth for the SSH
        # connection, the status-panel IP, and the daemon heartbeat URL.
        self.robot_host, self.robot_host_source = get_robot_host(robot_host)
        self._daemon_status_url = (
            f"http://{self.robot_host}:8000/api/daemon/status")
        self.state = SystemState.STARTING
        self.vad: SileroVAD | None = None
        self.robot: StreamingRobotPlayer | None = None
        self.client: genai.Client | None = None
        self.conversation: Conversation | None = None
        self._conv_task: asyncio.Task | None = None
        self._conv_log_handler = None
        self.error_message: str | None = None
        self.started_at = time.time()
        self._lock = asyncio.Lock()
        # Set by stop_system() while it drains a running conversation, so the
        # _run_conversation finally routes the post-conversation transition to
        # STOPPED (handled by stop_system) instead of auto-returning to
        # IDLE_BREATHING. Cleared once stop_system reaches STOPPED.
        self._pending_stop = False
        # Phase B heartbeat bookkeeping.
        self._heartbeat_task: asyncio.Task | None = None
        self._hb_misses = 0
        self._hb_connected = True  # last connected value broadcast via heartbeat

    # ----- broadcasting helpers -----

    def _broadcast(self, message: dict) -> None:
        try:
            self.broadcaster.broadcast_threadsafe(message)
        except Exception:
            log.exception("broadcast failed for %s", message.get("event"))

    def _set_state(self, state: SystemState, reason: str = "") -> None:
        old = self.state
        self.state = state
        log.info("system state %s -> %s (%s)", old.name, state.name, reason)
        self._broadcast({"event": "state.change", "state": state.name})
        # Robot status rides along with every state change so the UI panel
        # tracks connection without a separate poll (Phase A; Phase B adds a
        # periodic heartbeat).
        self._broadcast({"event": "robot.status", **self._robot_status()})

    def _robot_status(self) -> dict:
        connected = bool(self.robot is not None and self.robot.connected)
        return {
            "connected": connected,
            "ip": self.robot_host,
            # Phase A stub: no live daemon probe yet. "active" while the SSH
            # channel is up, "unknown" otherwise. Real probe deferred to B.
            "daemon_status": "active" if connected else "unknown",
        }

    # ----- robot daemon heartbeat (Phase B) -----

    def start_heartbeat(self) -> None:
        """Launch the heartbeat loop (idempotent). Called from the lifespan
        once the event loop is running."""
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        """Probe the robot daemon every HEARTBEAT_INTERVAL_S while the robot is
        meant to be up. Broadcasts robot.heartbeat each tick, and flips the
        Robot Connection card red (robot.status connected=false) after
        HEARTBEAT_MISS_LIMIT consecutive misses; restores it on recovery."""
        active = (SystemState.IDLE_BREATHING, SystemState.CONVERSATION_RUNNING)
        try:
            async with httpx.AsyncClient(timeout=HEARTBEAT_TIMEOUT_S) as client:
                while True:
                    await asyncio.sleep(HEARTBEAT_INTERVAL_S)
                    if self.state not in active:
                        # No probe while STOPPED/STARTING/SHUTTING_DOWN. Reset
                        # the miss counter so a fresh bring-up starts clean.
                        self._hb_misses = 0
                        self._hb_connected = True
                        continue
                    ok = False
                    try:
                        r = await client.get(self._daemon_status_url)
                        ok = (r.status_code == 200)
                    except Exception:
                        ok = False
                    if ok:
                        self._hb_misses = 0
                        self._broadcast({"event": "robot.heartbeat",
                                         "ok": True, "daemon_status": "active"})
                        if not self._hb_connected:
                            self._hb_connected = True
                            self._broadcast({"event": "robot.status",
                                             **self._robot_status()})
                    else:
                        self._hb_misses += 1
                        self._broadcast({"event": "robot.heartbeat", "ok": False})
                        if self._hb_misses >= HEARTBEAT_MISS_LIMIT and self._hb_connected:
                            self._hb_connected = False
                            status = self._robot_status()
                            status["connected"] = False
                            status["daemon_status"] = "unreachable"
                            self._broadcast({"event": "robot.status", **status})
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("heartbeat loop crashed")

    # ----- lifecycle -----

    async def startup(self) -> None:
        """Full process startup: STARTING -> IDLE_BREATHING. One-time init
        (API key, genai client, VAD) followed by the robot bring-up. On
        failure, go to STOPPED (fatal) and surface the reason to the UI.

        Split from the robot bring-up so start_system() can re-run only the
        robot side (Phase A.5 adjustment A): the VAD model and genai client
        are loaded once here and reused, never reloaded on a Start System."""
        self._set_state(SystemState.STARTING, "boot")
        try:
            api_key = await asyncio.to_thread(get_api_key)
        except SystemExit as e:
            return self._fatal(f"no API key: {e}")
        except Exception as e:
            return self._fatal(f"api key resolution failed: {e}")

        try:
            self.client = genai.Client(api_key=api_key)
        except Exception as e:
            return self._fatal(f"genai client init failed: {e}")

        log.info("Loading Silero VAD…")
        try:
            self.vad = await asyncio.to_thread(
                SileroVAD, SILERO_THRESHOLD, SILERO_FRAME_SIZE, GEMINI_INPUT_RATE)
        except Exception as e:
            return self._fatal(f"VAD load failed: {e}")

        # Resolve the mic up front so the chosen-device line lands in the idle
        # system log alongside VAD-load / robot-ready (instead of waiting for
        # the first conversation). Pure enumeration, no stream opened.
        try:
            await asyncio.to_thread(conv_mod._ensure_input_device)
        except Exception:
            log.exception("input device resolution failed; will retry lazily")

        await self._connect_robot()

    async def _connect_robot(self) -> bool:
        """Bring up the robot side and transition to IDLE_BREATHING. The caller
        has already set the state to STARTING. Reuses the already-loaded VAD
        and genai client. On failure, routes to STOPPED via _fatal and returns
        False; on success returns True.

        Blocking SSH connect is offloaded with asyncio.to_thread so the web
        server stays responsive (the page can show STARTING throughout)."""
        log.info("robot host %s (source: %s)",
                 self.robot_host, self.robot_host_source)
        log.info("Connecting to robot %s…", self.robot_host)
        try:
            # robot_log_path=None: robot stderr flows via the log_robot_stderr
            # logger to the system log (idle) / laptop.log (in conversation).
            self.robot = await asyncio.to_thread(
                StreamingRobotPlayer, self.robot_host, ROBOT_USER, ROBOT_PASSWORD, None)
        except Exception as e:
            self._fatal(f"robot connection failed: {e}")
            return False

        self._set_state(SystemState.IDLE_BREATHING, "robot_ready")
        log.info("System ready — IDLE_BREATHING. Robot breathing.")
        return True

    def _fatal(self, message: str) -> None:
        self.error_message = message
        log.error("Fatal startup error: %s", message)
        self._set_state(SystemState.STOPPED, "fatal")
        self._broadcast({"event": "error", "where": "startup", "message": message})

    async def start_conversation(self) -> dict:
        """IDLE_BREATHING -> CONVERSATION_RUNNING. Idempotent against double
        clicks via the state guard (raises instead of crashing)."""
        async with self._lock:
            if self.state == SystemState.CONVERSATION_RUNNING:
                raise ConversationConflict("already_running")
            if self.state != SystemState.IDLE_BREATHING:
                raise NotReady(self.state.name)
            assert self.client and self.vad and self.robot

            convo_dir = ConversationDir()
            conv = Conversation(
                self.client, self.vad, self.robot, convo_dir,
                on_transcript=self._on_transcript,
                on_turn_aborted=self._on_turn_aborted,
            )
            self.conversation = conv
            # Phase B: forward per-turn state transitions to the UI as
            # substates. The hook fires from both the event loop and the mic
            # worker thread, so the broadcast goes through broadcast_threadsafe.
            conv_mod.set_substate_hook(lambda raw: self._on_substate(conv, raw))
            # Attach the per-conversation laptop.log handler; base handlers
            # (console + system log + WS) stay in place and keep mirroring.
            self._conv_log_handler = open_conversation_log(convo_dir.dir)
            self._set_state(SystemState.CONVERSATION_RUNNING, "start_conversation")
            self._broadcast({
                "event": "conversation.started",
                "id": conv.id,
                "dir": str(convo_dir.dir),
            })
            self._conv_task = asyncio.create_task(self._run_conversation(conv))
            return {"conversation_id": conv.id, "dir": str(convo_dir.dir)}

    def _on_transcript(self, role: str, turn_id: int, text: str) -> None:
        event = "transcript.user" if role == "user" else "transcript.robot"
        self._broadcast({"event": event, "turn_id": turn_id, "text": text})

    def _on_turn_aborted(self, turn_id: int, reason: str) -> None:
        self._broadcast({"event": "turn.aborted", "turn_id": turn_id, "reason": reason})

    # Phase B substate mapping (corrected vs the spec's draft table): the human
    # is "being listened to" through both LISTENING (mic open) and CAPTURING
    # (actively speaking); THINKING is the real wait after audio is sent;
    # SPEAKING is audio flowing back. IDLE and anything unmapped emit nothing,
    # so the badge holds its last value through the brief between-turns gap.
    _SUBSTATE_MAP = {
        "LISTENING": "LISTENING",
        "CAPTURING": "LISTENING",
        "SENDING": "THINKING",
        "RECEIVING": "SPEAKING",
    }

    def _on_substate(self, conv: Conversation, raw_state: str) -> None:
        ui = self._SUBSTATE_MAP.get(raw_state)
        if ui is None:
            return
        self._broadcast({"event": "turn.substate", "state": ui, "turn_id": conv.turn})

    async def _run_conversation(self, conv: Conversation) -> None:
        """Wrapper around Conversation.run() that owns the transition back to
        IDLE_BREATHING on every exit path — normal end, end-phrase, or crash —
        so the system is never stuck in CONVERSATION_RUNNING (plan gotcha)."""
        reason = "user"
        try:
            reason = await conv.run()
        except asyncio.CancelledError:
            reason = "cancelled"
            raise
        except Exception as e:
            log.exception("Conversation crashed")
            reason = "crash"
            self._broadcast({"event": "error", "where": "gemini",
                             "message": f"{type(e).__name__}: {e}"})
        finally:
            conv_mod.set_substate_hook(None)
            close_conversation_log(self._conv_log_handler)
            self._conv_log_handler = None
            summary = conv.summary
            self.conversation = None
            self._conv_task = None
            # Auto-return to idle ONLY for an ordinary conversation end. Skip it
            # when shutdown set SHUTTING_DOWN, and skip it when stop_system set
            # _pending_stop (it owns the transition to STOPPED, avoiding a
            # visible IDLE_BREATHING flash between CONVERSATION_RUNNING and
            # STOPPED). In the _pending_stop case state stays CONVERSATION_RUNNING
            # here and stop_system flips it to STOPPED after robot teardown.
            if self.state == SystemState.CONVERSATION_RUNNING and not self._pending_stop:
                self._set_state(SystemState.IDLE_BREATHING, f"conversation_ended:{reason}")
            self._broadcast({
                "event": "conversation.ended",
                "reason": reason,
                "summary": summary,
            })

    async def _drain_conversation(self, reason: str) -> None:
        """Signal the running conversation to stop and await its run() task.
        No state guard and no lock (callers hold the guard); shared by the End
        button path (end_conversation) and the Stop System path (stop_system)."""
        conv = self.conversation
        task = self._conv_task
        if conv is not None:
            await conv.stop(reason)
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=40)
            except asyncio.TimeoutError:
                log.warning("Conversation did not stop within 40s; cancelling.")
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    async def end_conversation(self, reason: str = "user") -> dict:
        """CONVERSATION_RUNNING -> IDLE_BREATHING. Signals the conversation to
        stop and awaits its teardown (best-effort summary returned)."""
        if self.state != SystemState.CONVERSATION_RUNNING or self.conversation is None:
            raise ConversationConflict("not_running")
        conv = self.conversation
        await self._drain_conversation(reason)
        return {"summary": conv.summary}

    async def stop_system(self, reason: str = "user_stop") -> dict:
        """{IDLE_BREATHING, CONVERSATION_RUNNING} -> STOPPED. Tears down the
        robot connection while keeping the web server, VAD model, genai client,
        broadcaster, and WS clients alive.

        If a conversation is running, it is gracefully drained first (same path
        as End Conversation — no mid-turn audio cut), then the robot player is
        shut down: EOF on its stdin -> await process exit -> close SSH, all via
        robot.close() (blocking, offloaded). The whole operation holds the lock
        so a concurrent Start Conversation cannot race in during teardown."""
        async with self._lock:
            if self.state not in (SystemState.IDLE_BREATHING,
                                   SystemState.CONVERSATION_RUNNING):
                raise InvalidSystemState(self.state.name)
            self._pending_stop = True
            try:
                if self.state == SystemState.CONVERSATION_RUNNING:
                    # Implicit end. The _run_conversation finally sees
                    # _pending_stop and leaves the state CONVERSATION_RUNNING
                    # for us to flip to STOPPED below (no IDLE flash).
                    await self._drain_conversation(reason)
                if self.robot is not None:
                    try:
                        await asyncio.to_thread(self.robot.close)
                    except Exception:
                        log.exception("robot.close failed during stop_system")
                    self.robot = None
                self._set_state(SystemState.STOPPED, f"stop_system:{reason}")
            finally:
                self._pending_stop = False
        return {"state": self.state.name}

    async def start_system(self, reason: str = "user_start") -> dict:
        """STOPPED -> STARTING -> IDLE_BREATHING. Brings the robot side back up
        (fresh SSH + robot player) reusing the already-loaded VAD and client.
        Blocking: returns only once IDLE_BREATHING is reached (~7-10 s); on
        failure routes to STOPPED (retriable). The guard + STARTING transition
        run under the lock so a second Start System sees STARTING (409)."""
        async with self._lock:
            if self.state != SystemState.STOPPED:
                raise InvalidSystemState(self.state.name)
            self._set_state(SystemState.STARTING, f"start_system:{reason}")
        await self._connect_robot()
        return {"state": self.state.name}

    async def shutdown(self) -> None:
        """Stop any conversation, close SSH (EOF on stdin so the robot player
        exits cleanly), transition to SHUTTING_DOWN. Idempotent."""
        # Stop the heartbeat first so it can't probe a tearing-down robot.
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass
            self._heartbeat_task = None
        if self.state in (SystemState.SHUTTING_DOWN, SystemState.STOPPED):
            # Already torn down (or never came up). Still ensure robot closed.
            if self.robot is not None:
                await asyncio.to_thread(self.robot.close)
                self.robot = None
            return
        self._set_state(SystemState.SHUTTING_DOWN, "shutdown")

        conv = self.conversation
        task = self._conv_task
        if conv is not None:
            await conv.stop("shutdown")
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=20)
            except asyncio.TimeoutError:
                log.warning("Conversation did not stop within 20s on shutdown; cancelling.")
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        if self.robot is not None:
            # Order matters (plan gotcha): conversation stopped first (above),
            # now send EOF on stdin so the long-running player reads b"" and
            # exits its read loop. Blocking — run off the loop.
            try:
                await asyncio.to_thread(self.robot.close)
            except Exception:
                log.exception("robot.close failed")
            self.robot = None
        log.info("Shutdown complete.")

    # ----- status snapshot -----

    def status(self) -> dict:
        """The /api/status payload and the basis of the WS state.snapshot."""
        conv_info = None
        if self.conversation is not None:
            conv_info = {
                "id": self.conversation.id,
                "started_at": self.conversation.started_at,
                "turn_count": self.conversation.turn,
            }
        return {
            "state": self.state.name,
            "robot": self._robot_status(),
            "conversation": conv_info,
        }

    def snapshot(self) -> dict:
        """The full state.snapshot a WS client receives on connect: status +
        recent log tail + (if running) the transcript so far, so a refreshed
        browser tab reconstructs the view without restarting anything."""
        snap = {
            "event": "state.snapshot",
            **self.status(),
            "recent_log": self.broadcaster.recent_log(),
        }
        if self.conversation is not None:
            snap["transcript"] = list(self.conversation.transcript)
        if self.error_message:
            snap["error_message"] = self.error_message
        return snap
