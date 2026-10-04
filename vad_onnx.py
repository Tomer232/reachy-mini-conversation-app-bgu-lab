"""Silero-VAD via onnxruntime — the robot-side twin of ``vad.py``.

Same public surface as ``vad.SileroVAD`` (``feed_frame`` / ``is_speech`` /
``reset``) so ``record_with_vad`` cannot tell them apart. Exists because the
robot has no PyTorch and should not get it: torch for aarch64 is ~1 GB
installed against ~3.7 GB free on the SD card, whereas ``onnxruntime`` is
already in ``/venvs/mini_daemon`` and the silero-vad wheel already ships the
ONNX weights.

This is a numpy reimplementation of ``silero_vad.utils_vad.OnnxWrapper``,
which is written against torch tensors. The model contract it encodes:

  inputs   input  float32 (batch, context + frame) — 64 + 512 at 16 kHz
           state  float32 (2, batch, 128)          — carried across frames
           sr     int64 scalar
  outputs  prob   float32 (batch, 1)
           state  float32 (2, batch, 128)

The 64-sample context is the tail of the *previous* frame prepended to the
current one; the model is trained expecting it, and dropping it measurably
degrades the first frames of every utterance. ``reset()`` clears both the LSTM
state and the context, which is what keeps one turn's speech from bleeding
into the start of the next.

Kept deliberately dependency-light: numpy + onnxruntime, nothing else.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np


log = logging.getLogger("reachy.audio.vad")

_CONTEXT_SAMPLES_16K = 64
_CONTEXT_SAMPLES_8K = 32
_STATE_SHAPE = (2, 1, 128)

# Shipped alongside the sources so a deploy carries the weights; the installed
# wheel is the fallback for anyone running this on a machine that has it.
_BUNDLED_MODEL = Path(__file__).resolve().parent / "models" / "silero_vad.onnx"


def _resolve_model_path() -> str:
    if _BUNDLED_MODEL.is_file():
        return str(_BUNDLED_MODEL)
    try:
        from importlib import resources
        p = resources.files("silero_vad.data").joinpath("silero_vad.onnx")
        if Path(str(p)).is_file():
            return str(p)
    except Exception:
        pass
    raise FileNotFoundError(
        f"silero_vad.onnx not found at {_BUNDLED_MODEL} and the silero_vad "
        f"package is not installed. Copy the .onnx into models/ — see "
        f"tools/deploy_robot_app.py, which does this as part of a deploy."
    )


class SileroVADOnnx:
    """Per-frame voice-activity probability, onnxruntime backend.

    Drop-in for ``vad.SileroVAD``: same constructor signature, same three
    methods, same threshold semantics.
    """

    def __init__(
        self,
        threshold: float = 0.5,
        frame_size: int = 512,
        sample_rate: int = 16000,
    ) -> None:
        import onnxruntime

        self.threshold = float(threshold)
        self.frame_size = int(frame_size)
        self.sample_rate = int(sample_rate)

        if self.sample_rate == 16000:
            self._context_size = _CONTEXT_SAMPLES_16K
            expected_frame = 512
        elif self.sample_rate == 8000:
            self._context_size = _CONTEXT_SAMPLES_8K
            expected_frame = 256
        else:
            raise ValueError(
                f"sample_rate must be 8000 or 16000, got {self.sample_rate}")
        if self.frame_size != expected_frame:
            raise ValueError(
                f"frame_size must be {expected_frame} at {self.sample_rate} Hz, "
                f"got {self.frame_size} (Silero is strict about chunk size)")

        model_path = _resolve_model_path()
        log.info("Loading Silero VAD (onnx) from %s "
                 "(threshold=%.2f, frame=%d, sr=%d)…",
                 model_path, self.threshold, self.frame_size, self.sample_rate)

        # Single-threaded on purpose: the model is tiny, and on the robot this
        # runs beside a 100 Hz motion loop and an audio thread. Letting ORT
        # spawn a pool per session steals cores from work that has a deadline.
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        # Errors only. At the default level, creating a session on the robot
        # emits two GPU-discovery warnings ("Failed to detect devices under
        # /sys/class/drm/card0") because a Pi has no GPU to find. They are
        # harmless and they are printed before our logging config can route
        # them, so they land raw in the log the dashboard shows.
        opts.log_severity_level = 3
        self.session = onnxruntime.InferenceSession(
            model_path, providers=["CPUExecutionProvider"], sess_options=opts)

        self._sr_arr = np.array(self.sample_rate, dtype=np.int64)
        self.reset()

        # Warm up: the first run pays graph-init cost we don't want landing on
        # the first frame of the first turn.
        self.feed_frame(np.zeros(self.frame_size, dtype=np.float32))
        self.reset()
        log.info("Silero VAD ready (onnx)")

    def feed_frame(self, samples: "np.ndarray[Any, Any]") -> float:
        """One frame in, one speech probability out.

        Accepts int16 or float32, exactly like the torch wrapper: int16 is
        normalised by 32768.0. Frame length must equal ``self.frame_size``.
        """
        if samples.dtype == np.int16:
            arr = samples.astype(np.float32) / 32768.0
        elif samples.dtype == np.float32:
            arr = samples
        else:
            arr = samples.astype(np.float32)

        if arr.size != self.frame_size:
            raise ValueError(
                f"expected {self.frame_size} samples, got {arr.size}")

        # Prepend the previous frame's tail, then keep this frame's tail for
        # the next call — the sliding context the model was trained with.
        x = np.concatenate([self._context, arr]).reshape(1, -1)
        prob, state = self.session.run(
            None,
            {"input": x, "state": self._state, "sr": self._sr_arr},
        )
        self._state = state
        self._context = x[0, -self._context_size:]
        return float(prob.item())

    def is_speech(self, prob: float) -> bool:
        """Centralised threshold check. ``prob >= self.threshold``."""
        return prob >= self.threshold

    def reset(self) -> None:
        """Clear LSTM state and sliding context. Call between turns."""
        self._state = np.zeros(_STATE_SHAPE, dtype=np.float32)
        self._context = np.zeros(self._context_size, dtype=np.float32)
