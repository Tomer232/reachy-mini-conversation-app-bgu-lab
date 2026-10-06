#!/usr/bin/env python3
"""Camera vision: the robot sees through its head camera while it talks.

Tomer, 2026-10-05: "Reachy Mini has a camera and I want him to be able to see
me. In the desktop app he can see me, so why can't we?" The end goal is a
robot that can name what it sees ("what am I holding?", "what colour is my
shirt?").

**How the camera is reached, and why we never had to release anything.**
The daemon owns the camera (and the audio) through its GStreamer media server
(reachy_mini/media/media_server.py) and hands the frames out two ways:

  * an **IPC branch** -- raw frames on a unix socket,
    /tmp/reachymini_camera_socket (`unixfdsink`, capped at 10 fps), for apps
    running *on* the robot. This is what the SDK's MediaManager(LOCAL) reads
    through `GStreamerCamera`, and what Pollen's own conversation app uses via
    `reachy_mini.media.get_frame()`.
  * a **WebRTC branch** (`webrtcsink`, signalling on :8443) for remote
    clients -- that is how the desktop app shows the camera from the laptop.

Both are fan-out: any number of readers can attach, and reading takes nothing
from the daemon. So this module never calls /api/media/release (which would
stop the daemon's audio and broke later SDK clients) and never touches audio.
It attaches its own reader to the IPC socket, exactly like the SDK's
`GStreamerCamera._build_ipc_source` (source -> queue -> convert -> appsink),
plus a scale and a JPEG encoder, and a gate that lets one frame a second
through so the scale/encode costs ~nothing.

The approach -- latest frame kept in memory, JPEG to the model -- follows
Pollen's reachy_mini_conversation_app (camera_worker.py keeps the latest
frame; gemini_live.py sends frames with `send_realtime_input(video=...)`;
tools/camera.py + base_realtime.py put a frame into an OpenAI realtime
conversation as an `input_image`). Credit to them; the code is ours because
the robot's daemon venv has no OpenCV/PIL/PyAV, only GStreamer.

**Per backend** (wired in conversation.py and providers/gpt_live.py):

  * **Gemini Live** sees directly: the newest frame goes out with every turn,
    `send_realtime_input(video=<jpeg>)` just before the turn's audio.
    Verified 2026-10-05 on gemini-3.8-live and gemini-3.1-flash-live-preview
    (a ski photo: "red and black ski jacket ... holding a pair of ski poles").
  * **gpt-live-1 cannot take images** (OpenAI / Azure GPT-Live reference,
    2026-09: client events are audio, instructions, thinking, commentary).
    So a small side model (`SceneDescriber`, gpt-4.1-mini, ~1.7 s) describes
    the newest frame in two or three sentences and the description goes into
    the live session as quiet context (`session.thinking.append`) as each turn
    opens. It runs beside the conversation and never holds a reply up: a slow
    description lands late or not at all, and the robot answers without it.

**Privacy.** Frames live in memory only (the latest one) and in the requests
to the model; nothing is written to disk. The camera reader runs only while a
conversation with vision on is running.

Tunable: REACHY_VISION=0 (off everywhere), REACHY_VISION_MODEL,
REACHY_CAMERA_WIDTH x REACHY_CAMERA_HEIGHT (default 512x288), REACHY_CAMERA_FPS (default 1),
REACHY_CAMERA_IMAGE=<jpeg> (a still picture as the camera, for laptop tests).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("reachy.vision")

ENABLED = os.environ.get("REACHY_VISION", "1") != "0"

# The daemon's IPC endpoint (reachy_mini.daemon.utils.CAMERA_SOCKET_PATH).
CAMERA_SOCKET_PATH = "/tmp/reachymini_camera_socket"
TEST_IMAGE = os.environ.get("REACHY_CAMERA_IMAGE", "").strip()

WIDTH = int(os.environ.get("REACHY_CAMERA_WIDTH", "512"))
# 16:9, as the wireless camera's default 1280x720 (/api/camera/specs). Fixed
# rather than left to negotiation: 512x288 is what the 2026-10-05 probe on
# reachy3 read through this same chain. Another camera mode would be squashed,
# not refused.
HEIGHT = int(os.environ.get("REACHY_CAMERA_HEIGHT", "288"))
FPS = float(os.environ.get("REACHY_CAMERA_FPS", "1"))
JPEG_QUALITY = 80
# A frame older than this is not shown to a model: the camera has stalled
# and an old picture would be confidently wrong.
MAX_FRAME_AGE_S = 5.0
RETRY_S = 3.0
# No new frame for this long = the reader is rebuilt. Not seen to happen, but
# a one-off probe on reachy3 (2026-10-05, with a videorate element that this
# reader no longer has) got one frame in six seconds, and a rebuilt reader
# gets its first frame in ~0.15 s -- so a stall costs seconds, not the camera.
STALL_S = 3.0

API_URL = "https://api.openai.com/v1/chat/completions"
DESCRIBE_MODEL = os.environ.get("REACHY_VISION_MODEL", "gpt-4.1-mini")
DESCRIBE_TIMEOUT_S = 6.0


@dataclass(frozen=True)
class Frame:
    jpeg: bytes
    width: int
    height: int
    t: float          # time.monotonic() when captured
    seq: int

    @property
    def age_s(self) -> float:
        return time.monotonic() - self.t


def jpeg_size(jpeg: bytes) -> tuple:
    """(width, height) from a JPEG's frame header, (0, 0) if not found."""
    i = 2
    while i + 9 < len(jpeg) and jpeg[i] == 0xFF:
        marker, length = jpeg[i + 1], int.from_bytes(jpeg[i + 2:i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2):
            return (int.from_bytes(jpeg[i + 7:i + 9], "big"),
                    int.from_bytes(jpeg[i + 5:i + 7], "big"))
        i += 2 + length
    return 0, 0


def camera_source() -> tuple:
    """(kind, label) of the camera this process can use, or ("", why not)."""
    if TEST_IMAGE:
        if Path(TEST_IMAGE).is_file():
            return "image", "test image {}".format(Path(TEST_IMAGE).name)
        return "", "REACHY_CAMERA_IMAGE does not exist: {}".format(TEST_IMAGE)
    if os.path.exists(CAMERA_SOCKET_PATH):
        return "ipc", "robot camera"
    return "", "no camera here (the app is not running on the robot)"


class CameraFeed:
    """The newest camera frame, as a small JPEG, kept in memory.

    `start()` returns at once; frames arrive on a background thread. A
    reader that loses the socket (the daemon restarting its media) rebuilds
    itself every RETRY_S.
    """

    def __init__(self) -> None:
        self.kind, self.label = camera_source()
        self._latest: Optional[Frame] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seq = 0
        self.frames = 0

    @property
    def available(self) -> bool:
        return bool(self.kind)

    def start(self) -> "CameraFeed":
        if self.kind == "image":
            jpeg = Path(TEST_IMAGE).read_bytes()
            self._put(jpeg, *jpeg_size(jpeg))
            log.info("camera: %s stands in for the camera", self.label)
        elif self.kind == "ipc":
            self._thread = threading.Thread(target=self._run_ipc, name="camera",
                                            daemon=True)
            self._thread.start()
        return self

    def latest(self) -> Optional[Frame]:
        with self._lock:
            frame = self._latest
        if frame is None:
            return None
        if self.kind == "image":
            # A still picture never goes stale.
            return Frame(frame.jpeg, frame.width, frame.height, time.monotonic(), frame.seq)
        return frame if frame.age_s <= MAX_FRAME_AGE_S else None

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        with self._lock:
            self._latest = None       # nothing of the camera outlives the conversation

    # ----- internals -----

    def _put(self, jpeg: bytes, width: int, height: int) -> None:
        self._seq += 1
        self.frames += 1
        with self._lock:
            self._latest = Frame(jpeg, width, height, time.monotonic(), self._seq)

    def _run_ipc(self) -> None:
        try:
            import gi
            gi.require_version("Gst", "1.0")
            gi.require_version("GstApp", "1.0")
            from gi.repository import Gst
        except Exception as e:  # noqa: BLE001
            log.warning("camera off: GStreamer is not available here (%s)", e)
            return
        Gst.init(None)
        first = True
        stalls = 0
        while not self._stop.is_set():
            t0 = time.monotonic()
            last_frame = t0
            pipeline = None
            try:
                pipeline, sink = self._build(Gst)
                pipeline.set_state(Gst.State.PLAYING)
                bus = pipeline.get_bus()
                while not self._stop.is_set():
                    sample = sink.emit("try-pull-sample", 500_000_000)
                    if sample is None and time.monotonic() - last_frame > STALL_S:
                        stalls += 1
                        (log.warning if stalls in (1, 10, 100) else log.debug)(
                            "camera: no frame for %.0fs, rebuilding the reader "
                            "(stall %d)", STALL_S, stalls)
                        break
                    if sample is not None:
                        last_frame = time.monotonic()
                        buf = sample.get_buffer()
                        ok, info = buf.map(Gst.MapFlags.READ)
                        if ok:
                            jpeg = bytes(info.data)
                            buf.unmap(info)
                            s = sample.get_caps().get_structure(0)
                            w, h = s.get_value("width") or 0, s.get_value("height") or 0
                            self._put(jpeg, w, h)
                            if first:
                                first = False
                                log.info("camera: first frame %dx%d, %d bytes, after %.2fs",
                                         w, h, len(jpeg), time.monotonic() - t0)
                    msg = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
                    if msg is not None:
                        if msg.type == Gst.MessageType.ERROR:
                            err, _dbg = msg.parse_error()
                            raise RuntimeError(err.message)
                        raise RuntimeError("end of stream")
            except Exception as e:  # noqa: BLE001
                log.warning("camera reader stopped (%s); retrying in %.0fs", e, RETRY_S)
                self._stop.wait(RETRY_S)
            finally:
                if pipeline is not None:
                    pipeline.set_state(Gst.State.NULL)
        log.info("camera: reader closed after %d frames", self.frames)

    @staticmethod
    def _build(Gst):
        # As GStreamerCamera._build_ipc_source (the SDK's on-robot reader):
        # unixfdsrc -> queue -> convert -> appsink, here with a scale and a
        # JPEG encoder in front of the sink. No videorate: the gate below
        # does the rate, independent of buffer timestamps.
        desc = (
            "unixfdsrc socket-path={sock} ! "
            "queue name=gate leaky=downstream max-size-buffers=1 ! "
            "videoconvert ! videoscale ! "
            "video/x-raw,width={w},height={h} ! "
            "jpegenc quality={q} ! "
            "appsink name=sink drop=true max-buffers=1 sync=false"
        ).format(sock=CAMERA_SOCKET_PATH, w=WIDTH, h=HEIGHT, q=JPEG_QUALITY)
        pipeline = Gst.parse_launch(desc)
        sink = pipeline.get_by_name("sink")
        interval = 1.0 / max(FPS, 0.1)
        last = [0.0]

        def gate(_pad, _info):
            # Let one frame through per interval; the other nine a second are
            # dropped before they cost a conversion, a scale or an encode.
            now = time.monotonic()
            if now - last[0] < interval:
                return Gst.PadProbeReturn.DROP
            last[0] = now
            return Gst.PadProbeReturn.OK

        pipeline.get_by_name("gate").get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, gate)
        return pipeline, sink


# ----- the side model for a brain that cannot take images (gpt-live-1) -----

DESCRIBE_PROMPT = (
    "You are the eyes of Reachy, a small talking robot, describing what its "
    "camera sees right now so that Reachy can talk about it. In two or three "
    "short sentences, say concretely: the people (where they are, clothing "
    "and colours, what they hold or show, gestures, expression), the notable "
    "objects with their colours, any readable text, and the setting. Name "
    "things plainly. Never guess anyone's age, weight, ethnicity, health or "
    "other sensitive traits, and do not comment on bodies. If the picture is "
    "dark, blurred or blocked, say so in one sentence."
)


class SceneDescriber:
    """Turns a frame into a short description with a small vision model."""

    def __init__(self, api_key: str, model: str = DESCRIBE_MODEL) -> None:
        self._key = api_key
        self.model = model

    async def describe(self, frame: Frame) -> Optional[str]:
        """The description, or None (logged) if the model was slow or failed."""
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._ask, frame.jpeg),
                                          DESCRIBE_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001
            log.warning("vision: no description (%s: %s)", type(e).__name__, e)
            return None

    def _ask(self, jpeg: bytes) -> str:
        import base64
        body = {
            "model": self.model,
            "max_tokens": 140,
            "temperature": 0.2,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": DESCRIBE_PROMPT},
                {"type": "image_url", "image_url": {
                    "url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii"),
                    # 512 px is already small; "low" is ~550 tokens a frame.
                    "detail": "low"}},
            ]}],
        }
        req = urllib.request.Request(
            API_URL, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Authorization": "Bearer " + self._key,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=DESCRIBE_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return str(data["choices"][0]["message"]["content"]).strip()


# ----- what the robot is told about its eyes -----

_PROMPT = {
    "en": (
        "\n\nYou can see. A camera in your head shows you what is in front of "
        "you{how}. When someone asks what you see, what they are holding, "
        "wearing or showing you, or what colour something is, look and answer "
        "concretely: name the things and their colours. You may mention "
        "something you notice when it fits the conversation, but do not "
        "describe the picture unprompted. Be tactful about people: talk about "
        "clothes, objects, gestures and expressions, never guess anyone's "
        "age, weight, ethnicity or other sensitive traits, and do not comment "
        "on bodies. If the view is dark or unclear, say you cannot see well "
        "rather than guessing."
    ),
    "he": (
        "\n\nאתה יכול לראות. מצלמה בראש שלך מראה לך את מה שנמצא מולך{how}. "
        "כשמישהו שואל מה אתה רואה, מה הוא מחזיק, לובש או מראה לך, או באיזה "
        "צבע משהו — הסתכל וענה באופן מוחשי: קרא לדברים בשמם ובצבעיהם. אפשר "
        "להזכיר משהו שאתה שם לב אליו כשזה מתאים לשיחה, אבל אל תתאר את התמונה "
        "בלי שביקשו. היה עדין לגבי אנשים: דבר על בגדים, חפצים, תנועות והבעות, "
        "לעולם אל תנחש גיל, משקל, מוצא או תכונות רגישות אחרות, ואל תעיר על "
        "גוף. אם התמונה חשוכה או לא ברורה, אמור שאתה לא רואה טוב במקום לנחש."
    ),
}

_HOW = {
    ("en", "image"): "; the newest picture arrives with everything the person says",
    ("he", "image"): "; התמונה העדכנית מגיעה עם כל דבר שהאדם אומר",
    ("en", "notes"): ("; a short note describing what your camera sees right "
                      "now arrives as context as the person starts talking -- "
                      "the newest note replaces earlier ones. Speak about it as "
                      "what you see, not as a note you were given"),
    ("he", "notes"): ("; הערה קצרה שמתארת מה המצלמה שלך רואה עכשיו מגיעה כהקשר "
                      "כשהאדם מתחיל לדבר — ההערה החדשה מחליפה את הקודמות. דבר "
                      "עליה כמו על מה שאתה רואה, לא כמו על הערה שקיבלת"),
}


def prompt_addendum(language: str, how: str) -> str:
    """The lines appended to the system prompt when the robot can see.
    `how` is "image" (the brain gets pictures) or "notes" (descriptions)."""
    lang = "he" if str(language or "").lower().startswith("he") else "en"
    return _PROMPT[lang].format(how=_HOW[(lang, how)])
