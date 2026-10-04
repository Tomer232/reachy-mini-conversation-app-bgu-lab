"""List models the API key can see; filter to live-capable ones."""
import sys
from pathlib import Path
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
from google import genai

SCRIPT_DIR = Path(__file__).parent
def key():
    import os
    k = os.environ.get("GEMINI_API_KEY")
    if k:
        return k.strip()
    return (Path(__file__).resolve().parents[2] / "reachy-mini llm gemini token.txt").read_text().strip()

client = genai.Client(api_key=key())
print("=== Models with 'live' or 'audio' in name ===")
for m in client.models.list():
    name = m.name
    sm = getattr(m, "supported_actions", None) or getattr(m, "supported_generation_methods", None)
    if "live" in name.lower() or "audio" in name.lower():
        print(f"  {name}  actions={sm}")
print("\n=== All models (names only) ===")
for m in client.models.list():
    print(" ", m.name)
