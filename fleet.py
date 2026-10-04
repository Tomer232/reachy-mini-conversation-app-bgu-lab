#!/usr/bin/env python3
"""The roster -- which robots exist, and how to reach them.

Deploying to one robot was a hostname on a command line. Deploying to ten is a
list, and a list that lives in someone's head is a robot that silently misses
an update and then behaves differently from the other nine in front of an
audience.

    robots.json
    {
      "robots": [
        {"id": "unit-a1b2", "name": "Rina",  "host": "10.100.102.18"},
        {"id": "unit-c3d4", "name": "Dvir",  "host": "10.100.102.19"}
      ]
    }

`id` and `name` are the hub's -- this file is a convenience for the deploy
tools, not a second authority. The hub discovers ids over mDNS and owns the
id-to-name registry; what is written here should be a copy of that, and when
the two disagree the hub wins. Addresses in particular go stale the moment the
network changes, which is why every tool that reads this also accepts a plain
`--robot-host`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger("reachy.fleet")

SCRIPT_DIR = Path(__file__).parent
ROSTER_PATH = SCRIPT_DIR / "robots.json"


class Robot:
    __slots__ = ("id", "name", "host")

    def __init__(self, id: str = "", name: str = "", host: str = "") -> None:
        self.id = id
        self.name = name
        self.host = host

    @property
    def label(self) -> str:
        return self.name or self.id or self.host

    def __repr__(self) -> str:
        return "Robot({!r} @ {})".format(self.label, self.host)


def load(path: Path = ROSTER_PATH) -> list:
    """Every robot in the roster. An absent roster is an empty list, not an
    error -- a single-robot checkout never needs one."""
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read %s: %s", path.name, exc)
        return []
    entries = data.get("robots") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return []
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        host = str(e.get("host") or "").strip()
        if not host:
            # A roster entry with no address cannot be deployed to. Say which
            # one, rather than quietly deploying to nine of ten robots.
            log.warning("roster entry %r has no host and will be skipped",
                        e.get("name") or e.get("id") or "?")
            continue
        out.append(Robot(id=str(e.get("id") or ""),
                         name=str(e.get("name") or ""),
                         host=host))
    return out


def find(needle: str, path: Path = ROSTER_PATH) -> Optional[Robot]:
    """One robot by name, id or address -- whichever the person typed."""
    needle = (needle or "").strip().lower()
    for robot in load(path):
        if needle in (robot.id.lower(), robot.name.lower(), robot.host.lower()):
            return robot
    return None


def resolve_targets(hosts: Optional[str] = None, all_robots: bool = False,
                    default_host: str = "",
                    path: Path = ROSTER_PATH) -> list:
    """What a deploy tool should actually target.

    `hosts` is a comma-separated list of names, ids or addresses; `all_robots`
    means the whole roster. Anything not in the roster is still used as a plain
    address, so a robot that has just appeared on a new network can be deployed
    to before anyone gets round to writing it down.
    """
    if all_robots:
        robots = load(path)
        if not robots:
            raise SystemExit(
                "--all needs a roster: create {} (see fleet.py for the "
                "shape), or pass --robot-host".format(path.name))
        return robots
    if hosts:
        out = []
        for piece in str(hosts).split(","):
            piece = piece.strip()
            if not piece:
                continue
            found = find(piece, path)
            out.append(found or Robot(host=piece))
        if out:
            return out
    return [find(default_host, path) or Robot(host=default_host)]
