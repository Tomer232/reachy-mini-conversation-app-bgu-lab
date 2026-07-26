"""Show mode: fire pre-generated lines and movements at the robot on cue.

The counterpart to Conversation. Where Conversation gets audio from Gemini in
real time, ShowPlayer reads it off disk — so a cue is instant, identical every
time, and needs no network at all beyond the local link to the robot.

Everything ships over the existing wire protocol via StreamingRobotPlayer:
motion commands through send_motion_command(), audio through stream_chunk() /
end_turn(), interrupts through clear_playback(). No new message types, and the
robot player needs no changes — including the speech tapper, which sways the
head from whatever audio flows through it, so scripted lines look as alive as
live ones.

The cue audio is written by tools/build_show.py at 24 kHz mono int16, which is
exactly what stream_chunk() expects (it is Gemini's native output rate), so
there is no conversion on the playback path.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf

SCRIPT_DIR = Path(__file__).parent
SHOW_DIR = SCRIPT_DIR / "show"
CUES_PATH = SHOW_DIR / "cues.json"
MANIFEST_PATH = SHOW_DIR / "manifest.json"

# Audio is pushed at this multiple of realtime. Faster than realtime so the
# robot never starves mid-sentence; slow enough that the robot's buffer stays
# shallow, which is what makes Esc feel instant instead of cutting off audio
# that is already queued down there. 20 chunks of 4096 samples ~= 3.4 s of
# lookahead at 2x.
PACE_FACTOR = 2.0
CHUNK_SAMPLES = 4096          # 24 kHz frames per send (~170 ms)

log = logging.getLogger("reachy.show")


class ShowError(Exception):
    """Cue could not be fired (unknown id, missing audio, robot down)."""


class ShowPlayer:
    """Loads the cue file once, fires cues at the robot on demand.

    Owned by SystemManager for the process lifetime, like the robot connection.
    One cue plays at a time: firing while something is playing interrupts it.
    """

    def __init__(self, robot_getter, broadcast=None):
        # robot_getter is a callable rather than the robot itself because
        # SystemManager rebuilds the connection on Stop/Start System, and a
        # stale reference would silently send frames into a dead channel.
        self._robot_getter = robot_getter
        self._broadcast = broadcast
        self.cues: dict[str, dict] = {}
        self.sections: list[dict] = []
        self.manifest: dict = {"cues": {}}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_flag = threading.Event()
        self._playing: str | None = None
        self.load()

    # ----- cue file -----

    def load(self) -> None:
        """(Re)read cues.json + manifest.json. Safe to call at runtime; the
        operator board has a Reload button so a script edit does not need a
        restart."""
        if not CUES_PATH.exists():
            log.warning("no cue file at %s; show mode disabled", CUES_PATH)
            self.sections, self.cues = [], {}
            return
        data = json.loads(CUES_PATH.read_text(encoding="utf-8"))
        manifest = {"cues": {}}
        if MANIFEST_PATH.exists():
            try:
                manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            except Exception:
                log.exception("manifest unreadable; durations will be missing")
        self.manifest = manifest

        sections, cues = [], {}
        for section in data.get("sections", []):
            entries = []
            for cue in section.get("cues", []):
                cid = cue["id"]
                entry = manifest.get("cues", {}).get(cid, {})
                item = {
                    "id": cid,
                    "label": cue.get("label", cid),
                    "hotkey": cue.get("hotkey"),
                    "text": cue.get("text"),
                    "motion": cue.get("motion"),
                    "boss_cue": cue.get("boss_cue"),
                    "duration_s": entry.get("duration_s"),
                    "section": section["id"],
                    "has_audio": bool(cue.get("text")),
                    "audio_ready": bool(cue.get("text")) and (
                        SHOW_DIR / f"audio/{cid}.wav").exists(),
                }
                cues[cid] = item
                entries.append(item)
            sections.append({"id": section["id"], "title": section.get("title", ""),
                             "cues": entries})
        self.sections, self.cues = sections, cues
        missing = [c["id"] for c in cues.values() if c["has_audio"] and not c["audio_ready"]]
        log.info("show: %d cues in %d sections%s", len(cues), len(sections),
                 f"; MISSING AUDIO for {missing}" if missing else "")

    def catalog(self) -> dict:
        return {"sections": self.sections,
                "order": [c["id"] for s in self.sections for c in s["cues"]]}

    # ----- firing -----

    @property
    def playing(self) -> str | None:
        return self._playing

    def fire(self, cue_id: str) -> dict:
        """Fire one cue: motion immediately, then stream its audio if it has any.

        Interrupts whatever was playing. Returns the cue dict. Raises ShowError
        if the cue is unknown, its audio is missing, or the robot is down."""
        cue = self.cues.get(cue_id)
        if cue is None:
            raise ShowError(f"unknown cue {cue_id!r}")
        robot = self._robot_getter()
        if robot is None or not robot.connected:
            raise ShowError("robot not connected")

        with self._lock:
            self._stop_playback_locked(robot, clear_audio=cue["has_audio"])

            if cue["motion"]:
                try:
                    robot.send_motion_command(cue["motion"])
                except Exception as e:
                    raise ShowError(f"motion dispatch failed: {e}") from e

            if not cue["has_audio"]:
                log.info("cue %s: motion %s", cue_id, cue["motion"])
                self._emit("show.cue.started", cue)
                self._emit("show.cue.ended", cue)
                return cue

            wav = SHOW_DIR / f"audio/{cue_id}.wav"
            if not wav.exists():
                raise ShowError(f"no audio for {cue_id!r} — run tools/build_show.py")

            self._stop_flag.clear()
            self._playing = cue_id
            self._thread = threading.Thread(
                target=self._stream_worker, args=(robot, wav, cue),
                name=f"show-{cue_id}", daemon=True)
            self._thread.start()
            log.info("cue %s: %.1fs audio%s", cue_id, cue.get("duration_s") or 0,
                     f" + {cue['motion']}" if cue["motion"] else "")
            self._emit("show.cue.started", cue)
            return cue

    def stop(self) -> None:
        """Silence immediately and return the robot to breathing. The panic
        button — safe to call at any time, including with nothing playing."""
        robot = self._robot_getter()
        with self._lock:
            self._stop_playback_locked(robot, clear_audio=True)
            if robot is not None and robot.connected:
                try:
                    robot.send_motion_command({"type": "stop"})
                except Exception:
                    log.exception("stop: motion stop failed")
        log.info("show: stop")
        self._emit("show.stopped", None)

    def _stop_playback_locked(self, robot, clear_audio: bool) -> None:
        """Halt the streaming thread and drop audio already queued on the robot.

        Caller holds _lock. clear_audio is False for a motion-only cue so that
        firing a nod does not cut off a line that is still being spoken."""
        self._stop_flag.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
            if thread.is_alive():
                log.warning("show: streaming thread did not stop within 2s")
        self._thread = None
        if clear_audio and robot is not None and robot.connected:
            try:
                robot.clear_playback()
            except Exception:
                log.exception("clear_playback failed")
        self._playing = None

    def _stream_worker(self, robot, wav: Path, cue: dict) -> None:
        """Push the WAV to the robot in paced chunks, checking the stop flag
        between each so an interrupt lands within one chunk."""
        cue_id = cue["id"]
        try:
            samples, rate = sf.read(str(wav), dtype="int16")
            if samples.ndim > 1:
                samples = samples[:, 0]
            if rate != 24000:
                # stream_chunk assumes Gemini's native 24 kHz; anything else
                # would play at the wrong speed.
                log.error("cue %s: %d Hz audio, expected 24000 — refusing to play",
                          cue_id, rate)
                return

            chunk_wall_s = CHUNK_SAMPLES / 24000.0 / PACE_FACTOR
            next_send = time.monotonic()
            for start in range(0, samples.size, CHUNK_SAMPLES):
                if self._stop_flag.is_set():
                    log.debug("cue %s: interrupted at %.1fs", cue_id, start / 24000)
                    return
                robot.stream_chunk(np.ascontiguousarray(samples[start:start + CHUNK_SAMPLES]))
                next_send += chunk_wall_s
                sleep_s = next_send - time.monotonic()
                if sleep_s > 0:
                    time.sleep(sleep_s)
            if not self._stop_flag.is_set():
                robot.end_turn()   # flush the resampler tail
        except Exception:
            log.exception("cue %s: streaming failed", cue_id)
        finally:
            # Deliberately NOT taking _lock here. _stop_playback_locked joins
            # this thread while holding it, so grabbing it in the worker's exit
            # path deadlocks until the join times out — which showed up as a
            # 2-second lag on the STOP button, the one control that has to be
            # instant. Assignment is atomic under the GIL, and the guard makes
            # the only race benign: a newer cue owns _playing, so leave it.
            if self._playing == cue_id:
                self._playing = None
            self._emit("show.cue.ended", cue)

    # ----- plumbing -----

    def _emit(self, event: str, cue: dict | None) -> None:
        if self._broadcast is None:
            return
        payload = {"event": event}
        if cue is not None:
            payload.update({"cue_id": cue["id"], "label": cue["label"],
                            "duration_s": cue.get("duration_s"),
                            "has_audio": cue["has_audio"]})
        try:
            self._broadcast(payload)
        except Exception:
            log.exception("broadcast failed for %s", event)
