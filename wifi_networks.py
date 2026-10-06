"""WiFi networks the robot remembers -- so a lab member's own hotspot can be
added from the dashboard, without switching to it.

Each lab member brings their own phone hotspot. Adding it here *saves* it in
NetworkManager with autoconnect on, while the robot stays on the network it is
on now. Turn the current hotspot off and the new one on, and the robot moves
to it by itself; both stay remembered.

Not the daemon's `POST /wifi/connect`: that switches at once, and when the new
network is not in range it deletes it again and falls back to the robot's own
`reachy-mini-ap` setup network -- off the hotspot the dashboard is reached on.

Only on the robot (reachy_chat --local-robot). Changing system connections
needs root, and the app runs as `pollen`, so nmcli goes through `sudo -S`
with Pollen's factory password (override: REACHY_SUDO_PASSWORD). The WiFi
password is an nmcli argument for the instant the command runs -- acceptable
on a lab robot, and it is never logged.

Blocking (subprocess); call through a thread.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any

log = logging.getLogger("reachy.wifi")

FACTORY_SUDO_PASSWORD = "root"
NMCLI_TIMEOUT_S = 20
# The daemon's own setup network; shown, never offered for removal.
SETUP_PROFILE = "Hotspot"


class WifiError(RuntimeError):
    """A sentence the dashboard can show as it is."""


def _nmcli(args: list[str], sudo: bool = False) -> str:
    argv = (["sudo", "-S", "-p", "", "nmcli"] if sudo else ["nmcli"]) + args
    secret = (os.environ.get("REACHY_SUDO_PASSWORD") or FACTORY_SUDO_PASSWORD) + "\n"
    try:
        done = subprocess.run(argv, input=secret if sudo else None, capture_output=True,
                              text=True, timeout=NMCLI_TIMEOUT_S)
    except FileNotFoundError:
        raise WifiError("nmcli is not on this machine -- WiFi can only be set on the robot")
    except subprocess.TimeoutExpired:
        raise WifiError("NetworkManager did not answer within {} s".format(NMCLI_TIMEOUT_S))
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()
        reason = detail[-1] if detail else "exit {}".format(done.returncode)
        if "incorrect password" in reason.lower() or "sudo" in reason.lower():
            reason = "the robot refused sudo ({}) -- set REACHY_SUDO_PASSWORD".format(reason)
        raise WifiError(reason)
    return done.stdout


def _split(line: str) -> list[str]:
    """nmcli -t escapes ':' inside a field as '\\:'."""
    parts, cur, esc = [], "", False
    for ch in line:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


def _wifi_device() -> str:
    for line in _nmcli(["-t", "-f", "DEVICE,TYPE", "device"]).splitlines():
        parts = _split(line)
        if len(parts) >= 2 and parts[1] == "wifi":
            return parts[0]
    raise WifiError("this robot has no WiFi device")


def saved() -> list[dict[str, Any]]:
    """WiFi profiles NetworkManager remembers, the active one first."""
    out = []
    for line in _nmcli(["-t", "-f", "NAME,TYPE,AUTOCONNECT,ACTIVE",
                        "connection", "show"]).splitlines():
        parts = _split(line)
        if len(parts) < 4 or parts[1] != "802-11-wireless":
            continue
        out.append({"name": parts[0], "autoconnect": parts[2] == "yes",
                    "active": parts[3] == "yes", "setup": parts[0] == SETUP_PROFILE})
    out.sort(key=lambda n: (not n["active"], n["setup"], n["name"].lower()))
    return out


def save(ssid: str, password: str) -> dict[str, Any]:
    """Remember `ssid` with autoconnect on, without connecting to it now."""
    ssid = (ssid or "").strip()
    if not ssid or len(ssid.encode("utf-8")) > 32:
        raise WifiError("a WiFi name has 1 to 32 characters")
    if ssid == SETUP_PROFILE:
        raise WifiError("'{}' is the robot's own setup network".format(SETUP_PROFILE))
    if not 8 <= len(password or "") <= 63:
        raise WifiError("a WiFi password has 8 to 63 characters")
    if any(n["name"] == ssid for n in saved()):
        _nmcli(["connection", "modify", ssid,
                "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password,
                "connection.autoconnect", "yes"], sudo=True)
        action = "updated"
    else:
        _nmcli(["connection", "add", "type", "wifi", "con-name", ssid,
                "ifname", _wifi_device(), "ssid", ssid,
                "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password,
                "connection.autoconnect", "yes"], sudo=True)
        action = "saved"
    log.info("WiFi network %r %s (autoconnect on, not switched to)", ssid, action)
    return {"ok": True, "ssid": ssid, "action": action, "networks": saved()}
