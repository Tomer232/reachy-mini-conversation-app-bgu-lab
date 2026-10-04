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

import credentials as creds_mod
import identity as identity_mod
import providers as providers_mod
from backend import BackendStore, LANGUAGES
from persona import PersonaStore
from providers import elevenlabs_voice
import conversation as conv_mod
from conversation import (
    Conversation,
    ConversationDir,
    SessionSpec,
    StreamingRobotPlayer,
    SileroVAD,
    get_robot_host,
    ROBOT_USER,
    ROBOT_PASSWORD,
    GEMINI_MODEL,
    GEMINI_INPUT_RATE,
    SILERO_THRESHOLD,
    SILERO_FRAME_SIZE,
)
from logging_setup import open_conversation_log, close_conversation_log
from show_player import ShowPlayer, ShowError


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


class StartRefused(Exception):
    """Start Conversation cannot go ahead with the chosen backend -- usually a
    missing key. The message is for whoever is standing at the robot (HTTP
    400, body {error: "refused", detail})."""


class SystemManager:
    def __init__(self, broadcaster, robot_host: "str | None" = None,
                 robot_id: "str | None" = None,
                 robot_name: "str | None" = None,
                 provider: "str | None" = None,
                 api_key_id: "str | None" = None,
                 api_key: "str | None" = None):
        self.broadcaster = broadcaster
        # Robot SSH host, resolved once here at startup: --robot-host arg >
        # REACHY_ROBOT_HOST env > default. Single source of truth for the SSH
        # connection, the status-panel IP, and the daemon heartbeat URL.
        self.robot_host, self.robot_host_source = get_robot_host(robot_host)
        self._daemon_status_url = (
            f"http://{self.robot_host}:8000/api/daemon/status")

        # Who this instance is speaking for. Resolved before anything else so
        # every log line from here on can name its robot.
        self.identity = identity_mod.resolve(
            robot_id, robot_name, daemon_url=f"http://{self.robot_host}:8000")

        # Which backend, and on whose key. Both are settled at construction
        # rather than at conversation start: a robot that cannot reach a key
        # should say so on its dashboard while idle, not discover it when
        # somebody presses Start in front of an audience.
        self.provider = providers_mod.get(provider or providers_mod.DEFAULT_PROVIDER)
        self._requested_key_id = api_key_id
        self._requested_key = api_key
        self.credential = None
        self.credential_error: str | None = None

        # The dashboard's backend picker (backend.py). The launch provider
        # above is the one whose key must exist for the robot to come up at
        # all; the picker chooses per conversation, and a brain whose key is
        # missing is refused at Start with a sentence, not at boot.
        default_brain = next((b["id"] for b in providers_mod.BRAINS
                              if b["provider"] == self.provider.name),
                             providers_mod.DEFAULT_BRAIN)
        self.backend = BackendStore(default_brain=default_brain)
        self._clients: dict = {}
        self._el_voices: "list | None" = None
        self._el_voices_at = 0.0

        # The persona switch. Off by default; whatever was saved on this robot
        # is loaded here, which is what makes a persona survive a restart.
        self.persona = PersonaStore(provider=self._selected_provider().name)

        self.state = SystemState.STARTING
        self.vad: SileroVAD | None = None
        self.robot: StreamingRobotPlayer | None = None
        self.client = None
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
        # Show mode. Built at construction (reads cues.json + manifest.json off
        # disk, no robot needed) and given a getter rather than the robot
        # itself, because stop_system/start_system replace self.robot.
        self.show = ShowPlayer(lambda: self.robot,
                               broadcast=self.broadcaster.broadcast_threadsafe)
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
        if not self.provider.implemented:
            return self._fatal(
                f"{self.provider.display_name} is not built yet — relaunch "
                f"this robot with --provider gemini")
        try:
            self.credential = await asyncio.to_thread(
                creds_mod.resolve, self.provider.name,
                self._requested_key_id, self._requested_key)
        except creds_mod.NoCredential as e:
            self.credential_error = str(e)
            return self._fatal(str(e))
        except Exception as e:
            self.credential_error = str(e)
            return self._fatal(f"api key resolution failed: {e}")
        log.info("%s on key %s (%s)", self.provider.display_name,
                 self.credential.label or "environment", self.credential.source)

        try:
            self.client = self.provider.make_client(self.credential)
        except Exception as e:
            return self._fatal(f"{self.provider.display_name} client init failed: {e}")
        self._clients[self.provider.name] = self.client
        self._broadcast({"event": "backend.change", **self.backend_payload()})

        # Robot mode uses the onnxruntime backend: the robot has no PyTorch and
        # is not getting it (~1 GB against ~3.7 GB free). tools/test_vad_parity.py
        # showed the two backends agreeing to 0.000000 on bench_input_he.wav, so
        # this swap does not move any endpointing threshold.
        if conv_mod.LOCAL_ROBOT:
            from vad_onnx import SileroVADOnnx as _VADClass
            log.info("Loading Silero VAD (onnx backend, robot mode)…")
        else:
            _VADClass = SileroVAD
            log.info("Loading Silero VAD…")
        try:
            self.vad = await asyncio.to_thread(
                _VADClass, SILERO_THRESHOLD, SILERO_FRAME_SIZE, GEMINI_INPUT_RATE)
        except Exception as e:
            return self._fatal(f"VAD load failed: {e}")

        # Resolve the mic up front so the chosen-device line lands in the idle
        # system log alongside VAD-load / robot-ready (instead of waiting for
        # the first conversation). Pure enumeration, no stream opened.
        try:
            await asyncio.to_thread(conv_mod._ensure_input_device)
        except Exception as e:
            # On the laptop this is recoverable: resolution retries lazily and
            # the worst case is falling back to the default input. In robot
            # mode there is no acceptable fallback — the default input is the
            # robot's broken built-in mic — so a missing K11 is fatal here
            # rather than a conversation that silently never hears anything.
            if conv_mod.LOCAL_ROBOT:
                return self._fatal(f"microphone unavailable: {e}")
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

            # Settle the chosen backend before anything is created, so a
            # missing key refuses the start instead of half-starting it.
            spec, client = await self._prepare_session()

            convo_dir = ConversationDir()
            conv = Conversation(
                client, self.vad, self.robot, convo_dir,
                on_transcript=self._on_transcript,
                on_turn_aborted=self._on_turn_aborted,
                session=spec,
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
                "backend": self._describe_spec(spec),
            })
            log.info("Conversation on %s", self._describe_spec(spec))
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
                # Halt any cue first: its streaming thread writes to the SSH
                # channel we are about to close.
                self.show.stop()
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

    # ----- show mode -----

    def fire_cue(self, cue_id: str) -> dict:
        """Fire a show cue. Motion-only cues are allowed at any time, including
        mid-conversation — a nod while Gemini talks is exactly what an operator
        wants. Audio cues are allowed too, and deliberately so: the SAVE lines
        ("hold on", "say that again") exist precisely for when a live
        conversation stalls, and they interrupt whatever is being said. The
        operator can hear the room; the UI marks the interrupt risk rather than
        forbidding it.

        Blocking calls are trivial (a few frames on the SSH channel) except for
        the audio stream itself, which ShowPlayer runs on its own thread."""
        if self.state not in (SystemState.IDLE_BREATHING,
                              SystemState.CONVERSATION_RUNNING):
            raise InvalidSystemState(self.state.name)
        return self.show.fire(cue_id)

    def stop_cue(self) -> dict:
        self.show.stop()
        return {"stopped": True}

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
        # Same ordering rule as stop_system: no cue thread may outlive the SSH
        # channel it writes to.
        try:
            self.show.stop()
        except Exception:
            log.exception("show.stop failed during shutdown")
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

    # ----- the persona switch and what a session is built from -----

    def _session_spec(self, provider=None, credential=None,
                      voice_layer=None) -> SessionSpec:
        """Freeze the current persona and backend choice into the conversation
        about to start.

        Read once, here. The switches can be flicked while a conversation runs —
        they apply to the next one, and the dashboard says so.
        """
        state = self.persona.state
        choice = self.backend.state
        brain = providers_mod.brain(choice.brain)
        provider = provider or self._selected_provider()
        credential = credential if credential is not None else self.credential
        base_prompt, language_code = conv_mod.LANGUAGES.get(
            choice.language, conv_mod.LANGUAGES[conv_mod.DEFAULT_LANGUAGE])
        return SessionSpec(
            provider=provider,
            system_prompt=self.persona.prompt_for(base_prompt, choice.language),
            voice=self.persona.voice_for(provider.default_voice),
            language=language_code,
            model=brain["model"],
            persona_summary=state.summary(),
            robot=self.identity.to_dict(),
            credential=credential.public() if credential else {},
            voice_layer=voice_layer,
            brain_id=brain["id"],
        )

    # ----- the backend picker -----

    def _selected_provider(self):
        brain = providers_mod.brain(self.backend.state.brain)
        if brain["provider"] == self.provider.name:
            return self.provider
        return providers_mod.get(brain["provider"])

    def _credential_for(self, provider_name: str):
        """The key for one provider. The launch provider's was settled at boot
        (and may have come from the hub or --api-key); any other is looked up
        fresh each time, so a key added to keys.json mid-session is picked up
        without a restart. Raises creds_mod.NoCredential."""
        if provider_name == self.provider.name and self.credential is not None:
            return self.credential
        return creds_mod.resolve(provider_name, use_hub=False)

    _VENDOR = {creds_mod.GEMINI: ("Gemini", "GEMINI_API_KEY"),
               creds_mod.GPT_LIVE: ("OpenAI", "OPENAI_API_KEY"),
               creds_mod.ELEVENLABS: ("ElevenLabs", "ELEVENLABS_API_KEY")}

    def _key_status(self, provider_name: str) -> dict:
        try:
            cred = self._credential_for(provider_name)
        except creds_mod.NoCredential:
            vendor, env = self._VENDOR.get(provider_name, (provider_name, "?"))
            return {"ok": False,
                    "error": f"No {vendor} key yet — add one to keys.json or set {env}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "label": cred.label or cred.source,
                "tail": cred.tail, "source": cred.source}

    async def _prepare_session(self):
        """(SessionSpec, client) for the backend currently chosen. Raises
        StartRefused with a readable sentence on anything missing."""
        choice = self.backend.state
        brain = providers_mod.brain(choice.brain)
        provider = self._selected_provider()
        try:
            credential = await asyncio.to_thread(self._credential_for, provider.name)
        except creds_mod.NoCredential as e:
            raise StartRefused("{}: {}".format(
                brain["label"], self._key_status(provider.name).get("error") or e)) from e
        client = self._clients.get(provider.name)
        if client is None or provider.name != self.provider.name:
            try:
                client = provider.make_client(credential)
            except Exception as e:
                raise StartRefused(f"{brain['label']} client failed: {e}") from e
            self._clients[provider.name] = client

        voice_layer = None
        if choice.elevenlabs:
            try:
                el_cred = await asyncio.to_thread(
                    self._credential_for, creds_mod.ELEVENLABS)
            except creds_mod.NoCredential as e:
                raise StartRefused("ElevenLabs voice is on, but: {}".format(
                    self._key_status(creds_mod.ELEVENLABS).get("error") or e)) from e
            voice_id, voice_name = choice.el_voice_id, choice.el_voice_name
            if not voice_id:
                voices = await self.elevenlabs_voices()
                if not voices:
                    raise StartRefused(
                        "ElevenLabs voice is on, but no voice is chosen and the "
                        "account's voice list could not be read")
                voice_id, voice_name = voices[0]["voice_id"], voices[0]["name"]
            voice_layer = elevenlabs_voice.ElevenLabsVoice(
                el_cred.key, voice_id, choice.el_model, voice_name)

        spec = self._session_spec(provider=provider, credential=credential,
                                  voice_layer=voice_layer)
        return spec, client

    @staticmethod
    def _describe_spec(spec: SessionSpec) -> str:
        brain = providers_mod.brain(spec.brain_id) if spec.brain_id else {}
        voice = (spec.voice_layer.describe() if spec.voice_layer is not None
                 else f"{spec.provider.display_name} voice {spec.voice}")
        return "{} · {} · {}".format(brain.get("label", spec.model),
                                     spec.language, voice)

    async def elevenlabs_voices(self, refresh: bool = False) -> list:
        """The ElevenLabs account's voices, cached for ten minutes. Empty when
        there is no key or the list cannot be read (the reason is logged)."""
        if (not refresh and self._el_voices is not None
                and time.time() - self._el_voices_at < 600):
            return self._el_voices
        try:
            cred = await asyncio.to_thread(self._credential_for, creds_mod.ELEVENLABS)
            voices = await asyncio.to_thread(elevenlabs_voice.list_voices, cred.key)
        except Exception as e:
            log.warning("ElevenLabs voice list unavailable: %s", e)
            return self._el_voices or []
        self._el_voices = voices
        self._el_voices_at = time.time()
        return voices

    def backend_payload(self) -> dict:
        """Everything the backend picker renders."""
        choice = self.backend.state
        brains = []
        for b in providers_mod.BRAINS:
            key = self._key_status(b["provider"])
            note = ""
            if b["provider"] == "gpt_live":
                note = ("Hebrew is unverified on GPT-Live (OpenAI publishes no "
                        "language list). No motion tools: it talks and sways only.")
            brains.append({**b, "key_ok": key["ok"],
                           "key_error": key.get("error", ""),
                           "key_label": key.get("label", ""),
                           "key_tail": key.get("tail", ""),
                           "note": note})
        el_key = self._key_status(creds_mod.ELEVENLABS)
        return {
            "brain": choice.brain,
            "brains": brains,
            "language": choice.language,
            "languages": list(LANGUAGES),
            "elevenlabs": {
                "enabled": choice.elevenlabs,
                "key_ok": el_key["ok"],
                "key_error": el_key.get("error", ""),
                "voice_id": choice.el_voice_id,
                "voice_name": choice.el_voice_name,
                "model": choice.el_model,
                "models": list(elevenlabs_voice.MODELS),
            },
            "applies_next": self.state == SystemState.CONVERSATION_RUNNING,
        }

    def set_backend(self, **changes) -> dict:
        self.backend.update(**changes)
        # The persona panel's voice list follows the brain's provider.
        self.persona.provider = self._selected_provider().name
        payload = self.backend_payload()
        self._broadcast({"event": "backend.change", **payload})
        self._broadcast({"event": "persona.change", **self.persona_payload()})
        self._broadcast({"event": "identity.change", **self.identity_payload()})
        return payload

    def persona_payload(self) -> dict:
        """Everything the persona panel renders, in one place so the REST
        route and the WS snapshot can never drift apart."""
        state = self.persona.state
        provider = self._selected_provider()
        payload = state.public(provider.name)
        payload["presets"] = self.persona.presets()
        payload["voices"] = self.persona.voices()
        payload["default_voice"] = provider.default_voice
        payload["provider"] = provider.name
        payload["provider_display"] = provider.display_name
        payload["summary"] = state.summary()
        payload["active"] = state.is_active
        # True while a conversation is running: the panel uses it to say that
        # an edit lands on the next conversation rather than this one.
        payload["applies_next"] = (self.state == SystemState.CONVERSATION_RUNNING)
        return payload

    def set_persona(self, **changes) -> dict:
        state = self.persona.update(**changes)
        payload = self.persona_payload()
        self._broadcast({"event": "persona.change", **payload})
        return payload

    def reset_persona(self) -> dict:
        self.persona.reset()
        payload = self.persona_payload()
        self._broadcast({"event": "persona.change", **payload})
        return payload

    def identity_payload(self) -> dict:
        """Who this robot is and what it is talking through. Never the key.

        "What it is talking through" is the brain chosen for the next
        conversation, which is not always the provider it was launched on.
        """
        brain = providers_mod.brain(self.backend.state.brain)
        provider = self._selected_provider()
        if provider.name == self.provider.name:
            key = (self.credential.public() if self.credential
                   else {"error": self.credential_error or "not resolved yet"})
        else:
            try:
                key = self._credential_for(provider.name).public()
            except Exception as e:
                key = {"error": str(e)}
        display = brain["label"]
        if self.backend.state.elevenlabs:
            display += " + ElevenLabs voice"
        return {
            "robot": self.identity.to_dict(),
            "provider": {
                "name": provider.name,
                "display_name": display,
                "implemented": provider.implemented,
            },
            "key": key,
        }

    def status(self) -> dict:
        """The /api/status payload and the basis of the WS state.snapshot."""
        conv_info = None
        if self.conversation is not None:
            conv_info = {
                "id": self.conversation.id,
                "started_at": self.conversation.started_at,
                "turn_count": self.conversation.turn,
                "persona": self.conversation.session_spec.persona_summary,
            }
        return {
            "state": self.state.name,
            "robot": self._robot_status(),
            "conversation": conv_info,
            "identity": self.identity_payload(),
            "persona": self.persona_payload(),
            "backend": self.backend_payload(),
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
