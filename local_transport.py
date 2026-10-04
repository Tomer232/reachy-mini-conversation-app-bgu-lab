"""Run robot_streaming_player.py as a local child process instead of over SSH.

In robot mode this app runs *on* the robot, so the SSH hop to reach the player
is pointless — but the wire protocol, the player, and everything
``StreamingRobotPlayer`` does with them are the parts that have been debugged
against live hardware, and they are the last things worth rewriting before a
lecture.

So instead of a second player class, this module provides ``LocalPlayerChannel``:
a duck-typed stand-in for the exact slice of ``paramiko.Channel`` that
``StreamingRobotPlayer`` touches —

    send / closed / shutdown_write / recv_exit_status / close
    recv_stderr_ready / recv_stderr / exit_status_ready

— backed by ``subprocess.Popen``. The framing code, the sentinels, the stderr
drainer and the ready-detection stay byte-for-byte the same on both paths.

The child is the same ``~/scripts/robot_streaming_player.py`` the laptop path
execs over SSH. One copy of the player on the robot, driven two ways.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading


log = logging.getLogger("reachy.transport.local")

# How long close() waits for the player to exit after stdin EOF before it stops
# being polite. The player's own shutdown (stop_playing + mini.close) is well
# under a second; anything past this is a hang, not slowness.
_EXIT_GRACE_S = 10.0


class _NullClient:
    """Stands in for ``paramiko.SSHClient``, which the player closes on
    teardown. There is no connection to tear down here."""

    def close(self) -> None:
        pass


class LocalPlayerChannel:
    """A paramiko-Channel-shaped view of a local child process."""

    def __init__(self, argv: list[str], cwd: str | None = None) -> None:
        log.info("Starting local robot player: %s", " ".join(argv))
        self._proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,   # the player logs on stderr; stdout unused
            stderr=subprocess.PIPE,
            cwd=cwd,
            bufsize=0,                   # we frame our own writes; no buffering
        )
        self._closed = False
        self._stdin_closed = False

        # stderr is drained by a reader thread rather than select() so the
        # buffer semantics are the same on any platform, and so a slow
        # consumer can never fill the pipe and deadlock the player mid-turn.
        self._buf = bytearray()
        self._buf_lock = threading.Lock()
        self._eof = threading.Event()
        self._reader = threading.Thread(
            target=self._read_stderr, name="local-player-stderr", daemon=True)
        self._reader.start()

    # ----- stderr plumbing -----

    def _read_stderr(self) -> None:
        stream = self._proc.stderr
        if stream is None:
            self._eof.set()
            return
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                with self._buf_lock:
                    self._buf.extend(chunk)
        except Exception:
            log.exception("local player stderr reader died")
        finally:
            self._eof.set()

    def recv_stderr_ready(self) -> bool:
        with self._buf_lock:
            return len(self._buf) > 0

    def recv_stderr(self, nbytes: int) -> bytes:
        """Pop up to nbytes. Empty result means "nothing buffered right now",
        which is the same thing paramiko's non-blocking recv_stderr means."""
        with self._buf_lock:
            if not self._buf:
                return b""
            out = bytes(self._buf[:nbytes])
            del self._buf[:nbytes]
            return out

    # ----- lifecycle -----

    def exit_status_ready(self) -> bool:
        return self._proc.poll() is not None

    @property
    def closed(self) -> bool:
        return self._closed or self._proc.poll() is not None

    def send(self, data: bytes) -> int:
        """Write one framed message. Unlike paramiko's send, this always
        writes everything or raises — callers already assume completeness."""
        if self._stdin_closed or self._proc.stdin is None:
            raise OSError("local player stdin is closed")
        self._proc.stdin.write(data)
        try:
            self._proc.stdin.flush()
        except Exception:
            pass
        return len(data)

    def shutdown_write(self) -> None:
        """Close the child's stdin — its end-of-conversation signal, exactly
        like EOF on the SSH channel."""
        if self._stdin_closed:
            return
        self._stdin_closed = True
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
        except Exception:
            pass

    def recv_exit_status(self) -> int:
        try:
            return self._proc.wait(timeout=_EXIT_GRACE_S)
        except subprocess.TimeoutExpired:
            log.warning("local player did not exit within %.0fs of stdin EOF; "
                        "terminating", _EXIT_GRACE_S)
            self._proc.terminate()
            try:
                return self._proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                log.error("local player ignored SIGTERM; killing")
                self._proc.kill()
                return self._proc.wait()

    def close(self) -> None:
        self._closed = True
        self.shutdown_write()
        if self._proc.poll() is None:
            try:
                self.recv_exit_status()
            except Exception:
                pass
        # Let the reader drain the tail before the caller closes its log tee.
        self._eof.wait(timeout=1.0)
        try:
            if self._proc.stderr is not None:
                self._proc.stderr.close()
        except Exception:
            pass


def start_local_player(player_path: str,
                       python_exe: str | None = None,
                       ) -> tuple[_NullClient, LocalPlayerChannel]:
    """Spawn the player locally. Returns (client, channel) so the caller can
    assign both exactly as it does for the SSH path.

    ``python_exe`` defaults to the interpreter running this process, which in
    robot mode is already ``/venvs/mini_daemon/bin/python`` — the venv holding
    the reachy_mini SDK. Hardcoding ROBOT_PYTHON instead would be a trap for
    anyone who starts the app with a different interpreter.
    """
    if not os.path.isfile(player_path):
        raise FileNotFoundError(
            f"robot player not found at {player_path}. Deploy it with "
            f"tools/deploy_robot_player.py before starting robot mode.")
    exe = python_exe or sys.executable
    # -u: unbuffered, so 'ready' reaches the drainer immediately rather than
    # sitting in a pipe buffer until the timeout fires.
    return _NullClient(), LocalPlayerChannel([exe, "-u", player_path])
