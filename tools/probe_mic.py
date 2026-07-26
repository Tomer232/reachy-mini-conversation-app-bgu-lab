"""Throwaway audio-device probe. Lists every sounddevice device,
prints the full table, and for every input device tests whether
sd.check_input_settings(device=i, channels=1, samplerate=sr) passes
for sr in (16000, 44100, 48000).

Run from reachy_chat/.venv:
    .venv\\Scripts\\python.exe tools\\probe_mic.py
"""
from __future__ import annotations

import sys
import sounddevice as sd


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    devices = sd.query_devices()
    hostapis = sd.query_hostapis()

    print("== sd.query_hostapis() ==")
    for i, h in enumerate(hostapis):
        print(f"  [{i}] {h.get('name')}")
    print()

    print("== sd.query_devices() (full table) ==")
    header = f"{'idx':>3} {'host':<14} {'in':>3} {'out':>3} {'def_sr':>8}  name"
    print(header)
    print("-" * len(header))
    for i, d in enumerate(devices):
        hi = d.get("hostapi", -1)
        host_name = hostapis[hi]["name"] if 0 <= hi < len(hostapis) else "?"
        print(
            f"{i:>3} {host_name:<14} {d.get('max_input_channels',0):>3} "
            f"{d.get('max_output_channels',0):>3} "
            f"{d.get('default_samplerate',0):>8.0f}  {d.get('name','')}"
        )
    print()

    rates = (16000, 44100, 48000)
    print("== input devices: check_input_settings(channels=1) ==")
    print(f"{'idx':>3} {'host':<14}  16000  44100  48000   name")
    print("-" * 70)
    flagged: list[tuple[int, str, str, dict[int, bool]]] = []
    for i, d in enumerate(devices):
        if d.get("max_input_channels", 0) <= 0:
            continue
        hi = d.get("hostapi", -1)
        host_name = hostapis[hi]["name"] if 0 <= hi < len(hostapis) else "?"
        results: dict[int, bool] = {}
        for sr in rates:
            try:
                sd.check_input_settings(device=i, channels=1, samplerate=sr)
                results[sr] = True
            except Exception:
                results[sr] = False
        cells = "  ".join("OK   " if results[sr] else "FAIL " for sr in rates)
        print(f"{i:>3} {host_name:<14}  {cells}  {d.get('name','')}")
        flagged.append((i, host_name, d.get("name", ""), results))
    print()

    print("== candidate K11 / USB-mic matches ==")
    keywords = ("k11", "remax", "senxin", "usb")
    any_match = False
    for i, host_name, name, results in flagged:
        nl = name.lower()
        if any(k in nl for k in keywords):
            any_match = True
            note = " [WASAPI]" if host_name.lower() == "windows wasapi" else ""
            print(
                f"  idx={i} host={host_name}{note} name={name!r} "
                f"16k={results[16000]} 44.1k={results[44100]} 48k={results[48000]}"
            )
    if not any_match:
        print("  (none matched usb/k11/remax/senxin — inspect the table above)")

    print()
    print("== default input device ==")
    try:
        di = sd.default.device
        print(f"  sd.default.device = {di}")
        default_in = di[0] if isinstance(di, (tuple, list)) else di
        if default_in is not None and default_in >= 0:
            d = devices[default_in]
            print(f"  default input = idx {default_in}: {d.get('name')}")
    except Exception as e:
        print(f"  (unable to read default device: {e})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
