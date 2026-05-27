"""Silero-VAD wrapper.

Thin shim over the ``silero-vad`` PyPI package. Loads the model once at
construction, exposes a per-frame inference call, and centralises the
threshold comparison so callers don't sprinkle ``>= threshold`` checks.

Module is named ``vad.py`` rather than ``silero_vad.py`` deliberately:
the PyPI package this wraps is also named ``silero_vad``, and a local
module of the same name shadows it and breaks the import below.

Phase 4 architecture: the model is constructed once in ``main()`` and
reused across turns. Call ``reset()`` between turns to clear the model's
internal LSTM state — otherwise speech probabilities from one turn can
bleed into the start of the next.

Default model path is torch.jit (``silero_vad.load_silero_vad()`` with
no args). ONNX is opt-in and requires installing ``onnxruntime``
separately; we don't do that here.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import torch
from silero_vad import load_silero_vad


log = logging.getLogger("reachy.audio.vad")


class SileroVAD:
    """Per-frame voice-activity probability via Silero VAD.

    Public surface:
      - ``feed_frame(samples)`` -> raw speech probability in [0, 1]
      - ``is_speech(prob)``     -> threshold comparison
      - ``reset()``             -> clear the model's streaming LSTM state
    """

    def __init__(
        self,
        threshold: float = 0.5,
        frame_size: int = 512,
        sample_rate: int = 16000,
    ) -> None:
        self.threshold = float(threshold)
        self.frame_size = int(frame_size)
        self.sample_rate = int(sample_rate)
        log.info("Loading Silero VAD model (threshold=%.2f, frame=%d, sr=%d)…",
                 self.threshold, self.frame_size, self.sample_rate)
        # Cold load is ~120 ms on this CPU; weights ship inside the wheel
        # so there's no network fetch. Returns a torch.jit.RecursiveScriptModule.
        self.model = load_silero_vad()
        # Single warmup inference — the very first call has a JIT
        # specialization cost; running it once here keeps the first
        # real frame from paying it.
        with torch.no_grad():
            self.model(torch.zeros(self.frame_size, dtype=torch.float32),
                       self.sample_rate)
        self.model.reset_states()
        log.info("Silero VAD ready")

    def feed_frame(self, samples: "np.ndarray[Any, Any]") -> float:
        """One frame in, one speech probability out.

        Accepts int16 or float32. int16 is normalised to float32 in
        [-1, 1] by dividing by 32768.0. Frame length must equal
        ``self.frame_size`` (caller's responsibility — Silero is strict
        about chunk size; the supported set is {256, 512, 1024, 1536}).
        """
        if samples.dtype == np.int16:
            arr = samples.astype(np.float32) / 32768.0
        elif samples.dtype == np.float32:
            arr = samples
        else:
            arr = samples.astype(np.float32)
        tensor = torch.from_numpy(arr)
        with torch.no_grad():
            return float(self.model(tensor, self.sample_rate).item())

    def is_speech(self, prob: float) -> bool:
        """Centralised threshold check. ``prob >= self.threshold``."""
        return prob >= self.threshold

    def reset(self) -> None:
        """Clear the model's LSTM state. Call between turns."""
        self.model.reset_states()
