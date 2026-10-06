#!/usr/bin/env python3
"""Gestures for a brain that cannot call tools: gpt-live-1.

Gemini moves the robot by calling play_emotion / dance / move_head while it
talks. gpt-live-1 cannot do that in time: its tools sit behind a "delegation"
round trip that lands a gesture seconds after the words (providers/gpt_live.py),
so until 2026-10-05 a GPT-Live robot only swayed to its own speech. Tomer:
"we must have movement greater than swaying while he speaks."

So the gestures are chosen *beside* the conversation instead of inside it,
by a small fast text model picking from the same library Gemini uses (an
emotion, a dance or a head direction), sent straight to the robot:

  * **The first move fires the moment the reply starts**, judged mostly on
    what the person just said -- known before the robot opens its mouth. A
    pick takes ~1.6 s (gpt-4o-mini, measured 2026-10-05), so it lands early
    in the reply instead of after it.
  * **A second move** comes from the robot's own first full sentence, for
    longer replies. gpt-live-1 streams its words as an output transcript
    while it speaks.

Measured choices: a joke -> laughing1, "my dog died" -> calming1, "I got the
job" -> enthusiastic1, "let me think" -> thoughtful1.

Guarantees:
  * **It never holds up speech.** Picks run as background tasks; a slow or
    failed pick is dropped and logged, and the robot just keeps swaying.
  * **Restraint.** At most MAX_MOVES_PER_TURN moves per reply, MIN_GAP_S
    apart, never the same emotion twice running -- a robot that gestures on
    every sentence looks twitchy, not alive.
  * **Only known moves.** The model's answer is checked against the catalog;
    anything else is ignored.

Tunable without a code change: REACHY_DIRECTOR_MODEL, REACHY_DIRECTOR_MAX_MOVES,
REACHY_DIRECTOR_MIN_GAP_S, and REACHY_DIRECTOR=0 to turn it off.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.request
from typing import Callable, Optional

log = logging.getLogger("reachy.director")

API_URL = "https://api.openai.com/v1/chat/completions"
MODEL = os.environ.get("REACHY_DIRECTOR_MODEL", "gpt-4o-mini")
MAX_MOVES_PER_TURN = int(os.environ.get("REACHY_DIRECTOR_MAX_MOVES", "2"))
MIN_GAP_S = float(os.environ.get("REACHY_DIRECTOR_MIN_GAP_S", "3.0"))
PICK_TIMEOUT_S = 4.0
# A pick that comes back later than this after its sentence finished is
# stale -- the words it was meant to accompany are already over.
STALE_AFTER_S = 4.0
# Long run-on text is judged at a word break once it reaches this length.
SENTENCE_MAX_CHARS = 90
SENTENCE_ENDS = ".!?…"

ENABLED = os.environ.get("REACHY_DIRECTOR", "1") != "0"


def take_sentence(pending: str) -> tuple:
    """(a finished sentence or "", the text still being written)."""
    for i, ch in enumerate(pending):
        if ch in SENTENCE_ENDS and (i + 1 == len(pending) or pending[i + 1].isspace()):
            return pending[:i + 1].strip(), pending[i + 1:]
    if len(pending) >= SENTENCE_MAX_CHARS:
        space = pending.rfind(" ")
        if space > 0:
            return pending[:space].strip(), pending[space + 1:]
    return "", pending


def build_menu(emotions: list, dances: list, heads: list) -> str:
    """The move list the picker chooses from: each emotion with the first
    words of its own description. Kept short on purpose -- the full
    descriptions (10k characters) made a pick take 1.4-5.4 s."""
    lines = ["EMOTIONS (expressive head/antenna gestures):"]
    lines += ["- {}: {}".format(n, (d or "").split(".")[0].strip()[:45])
              for n, d in emotions]
    if dances:
        lines.append("DANCES (only for playful, upbeat or celebratory moments): "
                     + ", ".join("dance:" + n for n, _ in dances))
    lines.append("HEAD: " + ", ".join("head:" + h for h in heads))
    return "\n".join(lines)


SYSTEM_PROMPT = (
    "You choose body language for Reachy, a small expressive robot, while it "
    "speaks. You get what the person just said and the words Reachy is "
    "saying right now (sometimes only its first word or two -- then judge "
    "mostly by what the person said). Pick ONE move from the list that a lively, warm "
    "character would naturally make with that sentence -- an emotion that "
    "matches its feeling (joy, curiosity, surprise, thinking, empathy, "
    "agreement...), a dance only for genuinely playful moments, or a head "
    "move. Prefer gesturing over doing nothing when the sentence carries any "
    "feeling; answer none only for flat filler. Reply with JSON only: "
    '{"move": "<exact name from the list, or none>"}.\n\n'
)


class MotionDirector:
    """One per conversation. The turn loop calls start_turn / user_text /
    robot_text / end_turn; moves go out through `send(cmd)`."""

    def __init__(self, send: Callable[[dict], None], api_key: str,
                 emotions: list, dances: list, heads: list) -> None:
        self._send = send
        self._key = api_key
        self._emotions = {n for n, _ in emotions}
        self._dances = {n for n, _ in dances}
        self._heads = set(heads)
        self._system = SYSTEM_PROMPT + build_menu(emotions, dances, heads)
        self._pending = ""
        self._user = ""
        self._moves = 0
        self._last_move_t = 0.0
        self._last_trigger_t = 0.0
        self._last_emotion = ""
        # The move already made in this reply, so the picker can decide
        # whether a second one is called for (Tomer: "1 or 2 as he sees fit").
        self._turn_move = ""
        self._tasks: set = set()
        self._turn = 0
        self._reacted = False

    # ----- hooks the turn loop calls -----

    def start_turn(self) -> None:
        self._turn += 1
        self._pending, self._user, self._moves = "", "", 0
        self._turn_move = ""
        self._reacted = False

    def user_text(self, text: str) -> None:
        self._user = (self._user + text)[-400:]

    def robot_text(self, text: str) -> None:
        if not self._reacted:
            # The reply has just begun: react to what the person said now,
            # rather than waiting a whole sentence plus a pick.
            self._reacted = True
            self._maybe_pick(text.strip() or "...", min_len=0)
        self._pending += text
        while True:
            sentence, self._pending = take_sentence(self._pending)
            if not sentence:
                return
            self._maybe_pick(sentence)

    def end_turn(self) -> None:
        # The tail of the reply is still worth one move if the turn had none.
        if self._pending.strip() and self._moves == 0:
            self._maybe_pick(self._pending.strip())
        self._pending = ""

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

    # ----- choosing -----

    def _maybe_pick(self, sentence: str, min_len: int = 3) -> None:
        if self._moves >= MAX_MOVES_PER_TURN or len(sentence) < min_len:
            return
        # A sentence finishing within MIN_GAP_S of the last pick is skipped:
        # its move would land too close to that one and be refused anyway
        # (2026-10-05: "Sure!" ended with the reaction pick still running,
        # and the turn's second move was lost).
        now = time.monotonic()
        if now - self._last_trigger_t < MIN_GAP_S:
            return
        self._last_trigger_t = now
        # Reserve the slot now, so two quick sentences cannot both pass.
        self._moves += 1
        task = asyncio.get_running_loop().create_task(
            self._pick(sentence, self._user, self._turn, time.monotonic()))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _pick(self, sentence: str, user: str, turn: int, t0: float) -> None:
        try:
            move = await asyncio.wait_for(
                asyncio.to_thread(self._ask, sentence, user, self._turn_move),
                PICK_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001
            log.warning("director: no move for %r (%s)", sentence[:40],
                        type(e).__name__)
            self._release(turn)
            return
        now = time.monotonic()
        if turn != self._turn or now - t0 > STALE_AFTER_S:
            log.info("director: dropped stale move %s", move)
            self._release(turn)
            return
        cmd = self._command(move)
        if cmd is None or now - self._last_move_t < MIN_GAP_S:
            self._release(turn)
            return
        self._last_move_t = now
        if cmd["type"] == "emotion":
            self._last_emotion = cmd["name"]
        self._turn_move = move
        try:
            self._send(cmd)
            log.info("director: %s for %r (%.1fs)", move, sentence[:50], now - t0)
        except Exception as e:  # noqa: BLE001
            log.warning("director: send failed: %s", e)

    def _release(self, turn: int) -> None:
        """A reserved slot that produced no move goes back to the turn."""
        if turn == self._turn and self._moves > 0:
            self._moves -= 1

    def _command(self, move: str) -> Optional[dict]:
        move = (move or "").strip()
        if not move or move == "none":
            return None
        if move.startswith("dance:") and move[6:] in self._dances:
            return {"type": "dance", "name": move[6:]}
        if move.startswith("head:") and move[5:] in self._heads:
            return {"type": "head", "direction": move[5:]}
        if move in self._emotions and move != self._last_emotion:
            return {"type": "emotion", "name": move}
        return None

    @staticmethod
    def _question(sentence: str, user: str, already: str) -> dict:
        q = {"person_said": user[-300:], "reachy_says": sentence}
        if already:
            # A second move is optional: only when this sentence clearly
            # calls for a *different* gesture. Without this the picker,
            # told to prefer gesturing, added a second move nearly every time.
            q["already_did_this_reply"] = already
            q["note"] = ("Reachy already made that move a few seconds ago in "
                         "this reply. Add a second, different move only if this "
                         "sentence brings a new feeling or moment -- a "
                         "punchline, a celebration, a warm question, surprise. "
                         "If it just carries on in the same mood or explains "
                         "facts, answer none.")
        return q

    def _ask(self, sentence: str, user: str, already: str = "") -> str:
        body = {
            "model": MODEL,
            "temperature": 0.7,
            "max_tokens": 20,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": self._system},
                {"role": "user", "content": json.dumps(
                    self._question(sentence, user, already), ensure_ascii=False)},
            ],
        }
        req = urllib.request.Request(
            API_URL, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Authorization": "Bearer " + self._key,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=PICK_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["choices"][0]["message"]["content"]
        return str(json.loads(text).get("move", "none"))
