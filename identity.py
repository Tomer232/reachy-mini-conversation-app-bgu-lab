#!/usr/bin/env python3
"""Which robot is this? — the per-instance identity of one reachy_chat.

One `reachy_chat` used to mean one robot, so nothing ever had to say which
robot it was. With a fleet, every conversation, log line and dashboard header
has to name its own robot, and the name has to be the *same* name tomorrow.

Two separate things live here, and the difference matters:

  **id**   the robot's own hardware identity (Pollen's `unit_id`). Immutable,
           comes from the hardware, and is what a name is attached *to*.
  **name** what the lab calls that robot. Assigned once by a human and then
           permanent -- "from that moment forever".

The hub is the authority (see docs/HUB-INTERFACE.md): it discovers the id over
mDNS before anything is launched, holds the id -> name registry, and passes
both in at launch. This module is the receiving end, plus two fallbacks so a
hand-started instance is never anonymous:

    --robot-id / --robot-name      what the hub passes
    robot_identity.json            what the last launch cached, on this robot
    the local daemon               the id alone, when nothing else knows it

A rename is possible but never silent: replacing a cached name logs a warning
naming both, because a robot that quietly becomes a different robot is exactly
how two demo transcripts end up attributed to the wrong body.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("reachy.identity")

SCRIPT_DIR = Path(__file__).parent
IDENTITY_PATH = SCRIPT_DIR / "robot_identity.json"

# Keys the Pollen daemon might carry its unit id under. UNVERIFIED against
# hardware -- the hub reads `unit_id` from the mDNS TXT record, and whether the
# daemon's own status echoes it has never been checked (no robot was reachable
# when this was written). Tried in order; all missing is not an error, it just
# means the id stays unknown until the hub supplies one.
_DAEMON_ID_KEYS = ("unit_id", "unitId", "serial_number", "serial", "uid")

UNKNOWN_ID = "unknown"


@dataclass
class RobotIdentity:
    """Who this instance is speaking for."""

    robot_id: str = UNKNOWN_ID
    name: str = ""
    source: str = "default"     # where the values came from, for the log

    @property
    def display_name(self) -> str:
        """Never blank. A robot with no assigned name is shown by id, and a
        robot with neither is shown as 'Reachy' rather than an empty header."""
        if self.name:
            return self.name
        if self.robot_id and self.robot_id != UNKNOWN_ID:
            return self.robot_id
        return "Reachy"

    @property
    def is_named(self) -> bool:
        return bool(self.name)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["display_name"] = self.display_name
        return d


def _read_cache(path: Path = IDENTITY_PATH) -> Optional[RobotIdentity]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read %s (%s); ignoring the cached identity",
                    path.name, exc)
        return None
    if not isinstance(data, dict):
        return None
    return RobotIdentity(
        robot_id=str(data.get("robot_id") or UNKNOWN_ID),
        name=str(data.get("name") or ""),
        source="cache",
    )


def _write_cache(identity: RobotIdentity, path: Path = IDENTITY_PATH) -> None:
    payload = {"robot_id": identity.robot_id, "name": identity.name}
    try:
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        # A read-only or full filesystem must not stop a conversation starting.
        log.warning("could not cache the identity to %s: %s", path.name, exc)


def id_from_daemon(base_url: str, timeout: float = 3.0) -> Optional[str]:
    """Best-effort unit id from the robot's own daemon. None on any failure.

    Only ever a fallback: it fills in the id when no one passed one, and it
    never supplies a name -- a name is a human decision, not a hardware fact.
    """
    try:
        import httpx
        r = httpx.get(f"{base_url.rstrip('/')}/api/daemon/status", timeout=timeout)
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        log.debug("daemon identity probe failed: %s", exc)
        return None
    if not isinstance(data, dict):
        return None
    for key in _DAEMON_ID_KEYS:
        value = data.get(key)
        if value:
            return str(value)
    # Some daemons nest the interesting fields one level down.
    for value in data.values():
        if isinstance(value, dict):
            for key in _DAEMON_ID_KEYS:
                if value.get(key):
                    return str(value[key])
    return None


def resolve(arg_id: Optional[str] = None,
            arg_name: Optional[str] = None,
            daemon_url: Optional[str] = None,
            path: Path = IDENTITY_PATH) -> RobotIdentity:
    """Settle this instance's identity, and cache it for the next hand-start.

    Precedence: what the launcher passed, then what the last launch cached,
    then the daemon for the id alone.
    """
    cached = _read_cache(path)
    identity = RobotIdentity()
    sources: list[str] = []

    if arg_id:
        identity.robot_id = str(arg_id)
        sources.append("id:arg")
    elif cached and cached.robot_id != UNKNOWN_ID:
        identity.robot_id = cached.robot_id
        sources.append("id:cache")
    elif daemon_url:
        probed = id_from_daemon(daemon_url)
        if probed:
            identity.robot_id = probed
            sources.append("id:daemon")

    if arg_name:
        identity.name = str(arg_name)
        sources.append("name:arg")
    elif cached and cached.name:
        identity.name = cached.name
        sources.append("name:cache")

    identity.source = ",".join(sources) or "default"

    # A rename is legal -- the hub owns the registry -- but it is never silent.
    if (cached and cached.name and identity.name
            and cached.name != identity.name
            and cached.robot_id == identity.robot_id):
        log.warning("robot %s was named '%s' and is now being called '%s' -- "
                    "if that was not deliberate, the hub's name registry and "
                    "this robot disagree", identity.robot_id, cached.name,
                    identity.name)

    if identity.robot_id != UNKNOWN_ID or identity.name:
        if cached is None or (cached.robot_id, cached.name) != (identity.robot_id, identity.name):
            _write_cache(identity, path)

    if not identity.is_named:
        log.info("this robot has no assigned name (id: %s) -- the hub passes "
                 "--robot-name; until then the dashboard shows the id",
                 identity.robot_id)
    else:
        log.info("this is %s (id: %s, via %s)", identity.name,
                 identity.robot_id, identity.source)
    return identity
