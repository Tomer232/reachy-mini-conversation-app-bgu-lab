#!/usr/bin/env python3
"""Offline checks for camera vision (vision.py), no robot, no network.

    .venv\\Scripts\\python.exe tools\\test_vision.py

  1. a still picture as the camera (REACHY_CAMERA_IMAGE's path)
  2. the robot's IPC reader, against a fake GStreamer: frames are kept, the
     newest wins, a stalled reader is rebuilt, close() forgets the picture
  3. gpt-live-1: a camera note goes out as session.thinking.append, at most
     one describe at a time, and a failed describe sends nothing
  4. the prompt lines, both languages

The live checks are tools/check_backends.py --see <photo.jpg> <combo>.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import types as pytypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import vision  # noqa: E402

# A 2x3 JPEG header is all jpeg_size needs: SOI, then SOF0 with h=3, w=2.
TINY_JPEG = bytes([0xFF, 0xD8, 0xFF, 0xC0, 0x00, 0x11, 0x08, 0x00, 0x03,
                   0x00, 0x02, 0x03]) + b"\x00" * 16

FAILS = []


def check(name, ok, detail=""):
    print("[{}] {}{}".format("PASS" if ok else "FAIL", name,
                             "  ({})".format(detail) if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def test_still_image():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "cam.jpg"
        p.write_bytes(TINY_JPEG)
        vision.TEST_IMAGE = str(p)
        try:
            feed = vision.CameraFeed()
            check("still image is a camera", feed.available and feed.kind == "image")
            feed.start()
            f = feed.latest()
            check("still image frame", f is not None and f.jpeg == TINY_JPEG
                  and (f.width, f.height) == (2, 3), str(f and (f.width, f.height)))
            feed.close()
            check("close forgets the frame", feed.latest() is None)
        finally:
            vision.TEST_IMAGE = ""


class _FakeGst:
    """Just enough of gi.repository.Gst for CameraFeed._run_ipc/_build."""

    class State:
        PLAYING, NULL = "PLAYING", "NULL"

    class MapFlags:
        READ = 1

    class MessageType:
        ERROR, EOS = 1, 2

    class PadProbeType:
        BUFFER = 1

    class PadProbeReturn:
        OK, DROP = "OK", "DROP"

    builds = 0
    script = []        # per build: list of jpeg bytes or None (= no sample)

    @staticmethod
    def init(_):
        pass

    @classmethod
    def parse_launch(cls, desc):
        assert "unixfdsrc" in desc and "jpegenc" in desc, desc
        cls.builds += 1
        plan = list(cls.script.pop(0)) if cls.script else []
        return _FakePipeline(plan)


class _FakePipeline:
    def __init__(self, plan):
        self.plan = plan
        self.probe = None

    def get_by_name(self, name):
        pipe = self

        class _El:
            def emit(self, _sig, _timeout):
                if pipe.plan:
                    item = pipe.plan.pop(0)
                    if item is None:
                        time.sleep(0.05)
                        return None
                    return _FakeSample(item)
                time.sleep(0.05)
                return None

            def get_static_pad(self, _n):
                class _Pad:
                    def add_probe(self_inner, _t, fn):
                        pipe.probe = fn
                return _Pad()
        return _El()

    def set_state(self, _s):
        pass

    def get_bus(self):
        class _Bus:
            def pop_filtered(self, _m):
                return None
        return _Bus()


class _FakeSample:
    def __init__(self, data):
        self.data = data

    def get_buffer(self):
        sample = self

        class _Buf:
            def map(self, _f):
                return True, pytypes.SimpleNamespace(data=sample.data)

            def unmap(self, _i):
                pass
        return _Buf()

    def get_caps(self):
        class _Caps:
            def get_structure(self, _i):
                return pytypes.SimpleNamespace(
                    get_value=lambda k: {"width": 512, "height": 288}[k])
        return _Caps()


def test_ipc_reader():
    fake_repo = pytypes.ModuleType("gi.repository")
    fake_repo.Gst = _FakeGst
    fake_gi = pytypes.ModuleType("gi")
    fake_gi.require_version = lambda *_a: None
    fake_gi.repository = fake_repo
    saved = {k: sys.modules.get(k) for k in ("gi", "gi.repository")}
    sys.modules["gi"], sys.modules["gi.repository"] = fake_gi, fake_repo
    old_stall = vision.STALL_S
    vision.STALL_S = 0.3
    try:
        # Build 1: two frames, then nothing (a stall); build 2: one more frame.
        _FakeGst.builds = 0
        _FakeGst.script = [[b"f1", b"f2"] + [None] * 40, [b"f3"] + [None] * 400]
        feed = vision.CameraFeed()
        feed.kind, feed.label = "ipc", "fake"
        feed.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (feed.latest() is None
                                               or feed.latest().jpeg != b"f3"):
            time.sleep(0.05)
        f = feed.latest()
        check("reader keeps the newest frame", f is not None and f.jpeg == b"f3",
              repr(f and f.jpeg))
        check("stalled reader is rebuilt", _FakeGst.builds >= 2, str(_FakeGst.builds))
        check("frame size from caps", f is not None and (f.width, f.height) == (512, 288))
        feed.close()
        check("reader stops and forgets", feed._thread is None and feed.latest() is None)
    finally:
        vision.STALL_S = old_stall
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    # The gate: one buffer per interval.
    class G:
        PadProbeReturn = _FakeGst.PadProbeReturn
        PadProbeType = _FakeGst.PadProbeType
        parse_launch = _FakeGst.parse_launch
    pipe, _sink = vision.CameraFeed._build(G)
    results = [pipe.probe(None, None) for _ in range(10)]
    check("gate passes one frame a second", results.count("OK") == 1, str(results))


class _FakeWs:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


class _Feed:
    def latest(self):
        return vision.Frame(TINY_JPEG, 2, 3, time.monotonic(), 1)


class _Describer:
    def __init__(self, text, delay=0.05):
        self.text, self.delay, self.calls = text, delay, 0

    async def describe(self, _frame):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return self.text


async def _gpt_live():
    from providers.gpt_live import GptLiveSession
    s = GptLiveSession("k", {})
    s._ws = _FakeWs()
    d = _Describer("A person in a red jacket holding a blue mug.")
    s.attach_vision(_Feed(), d)
    s._look("turn")            # a describe is already running: ignored
    await asyncio.sleep(0.2)
    notes = [m for m in s._ws.sent if m["type"] == "session.thinking.append"]
    check("one camera note sent", len(notes) == 1 and d.calls == 1,
          "{} notes, {} calls".format(len(notes), d.calls))
    check("note is general context", notes and notes[0]["delegation_id"] is None
          and "red jacket" in notes[0]["content"])
    s._look("turn")            # within LOOK_MIN_GAP_S: ignored
    await asyncio.sleep(0.1)
    check("no note sooner than the gap", d.calls == 1)

    s2 = GptLiveSession("k", {})
    s2._ws = _FakeWs()
    s2.attach_vision(_Feed(), _Describer(None))
    await asyncio.sleep(0.2)
    check("failed describe sends nothing", not s2._ws.sent)

    # A delegation is answered with the latest view.
    await s._decline_delegation(s._ws, {"delegation": {"id": "d1"}})
    check("delegation answered with the view", "red jacket" in s._ws.sent[-1]["content"]
          and s._ws.sent[-1]["delegation_id"] == "d1")


def test_prompt():
    he = vision.prompt_addendum("he-IL", "image")
    en = vision.prompt_addendum("en-US", "notes")
    check("prompt: hebrew", "מצלמה" in he and "{how}" not in he)
    check("prompt: english notes", "note" in en and "age, weight" in en)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    check("jpeg size", vision.jpeg_size(TINY_JPEG) == (2, 3))
    test_still_image()
    test_ipc_reader()
    asyncio.run(_gpt_live())
    test_prompt()
    print("\n{} failed".format(len(FAILS)) if FAILS else "\nall passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
