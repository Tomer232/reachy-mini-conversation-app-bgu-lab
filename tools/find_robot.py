"""Find the Reachy Mini on the current network.

The robot's address changes whenever it joins a different WiFi, and
reachy-mini.local can stay pinned to a stale address on Windows. This scans
the laptop's own /24 for the robot daemon (port 8000) and confirms each hit by
reading /api/daemon/status.

    .venv\\Scripts\\python.exe tools\\find_robot.py
"""
from __future__ import annotations

import concurrent.futures
import json
import socket
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DAEMON_PORT = 8000
CONNECT_TIMEOUT_S = 0.4
HTTP_TIMEOUT_S = 2.0


def local_ipv4() -> str | None:
    """The address this laptop uses to reach the outside world."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def port_open(ip: str) -> str | None:
    try:
        with socket.create_connection((ip, DAEMON_PORT), timeout=CONNECT_TIMEOUT_S):
            return ip
    except Exception:
        return None


def identify(ip: str) -> dict | None:
    try:
        with urllib.request.urlopen(
                f"http://{ip}:{DAEMON_PORT}/api/daemon/status", timeout=HTTP_TIMEOUT_S) as r:
            data = json.loads(r.read().decode())
    except Exception:
        return None
    if data.get("type") != "daemon_status":
        return None
    return data


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    me = local_ipv4()
    if me is None:
        print("Could not determine this laptop's IP. Is WiFi connected?")
        return 1
    subnet = me.rsplit(".", 1)[0]
    print(f"This laptop: {me}")
    print(f"Scanning {subnet}.1-254 for a Reachy Mini daemon on port {DAEMON_PORT}…")

    candidates: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=128) as pool:
        for hit in pool.map(port_open, [f"{subnet}.{i}" for i in range(1, 255)]):
            if hit:
                candidates.append(hit)

    found = []
    for ip in candidates:
        info = identify(ip)
        if info:
            found.append((ip, info))

    if not found:
        print("\nNo robot found on this network.")
        print("Check that the robot is powered on and joined to the same WiFi as")
        print("this laptop (it will not be reachable over a phone hotspot unless")
        print("the laptop is on that hotspot too).")
        return 1

    print()
    for ip, info in found:
        print(f"Found: {info.get('robot_name', 'reachy')} at {ip}")
        print(f"  state   : {info.get('state')}")
        print(f"  version : {info.get('version')}")
        print(f"  wlan_ip : {info.get('wlan_ip')}")
    ip = found[0][0]
    print("\nStart the dashboard with:")
    print(f"    .\\.venv\\Scripts\\python.exe laptop_chat.py --robot-host {ip}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
