"""Probe google.genai types for Live config shape and async session API."""
from google.genai import types
from google import genai
import inspect

# Look for relevant types
for name in [
    "LiveConnectConfig",
    "SpeechConfig",
    "VoiceConfig",
    "PrebuiltVoiceConfig",
    "AudioTranscriptionConfig",
    "Blob",
    "Content",
    "Part",
]:
    t = getattr(types, name, None)
    print(f"--- {name} ---")
    if t is None:
        print("  (not found)")
        continue
    try:
        print("  fields:", list(t.model_fields.keys()) if hasattr(t, "model_fields") else "n/a")
    except Exception as e:
        print(f"  err: {e}")

# Async session methods on client.aio.live
print("\n--- client.aio.live methods ---")
client = genai.Client(api_key="dummy")
live = client.aio.live
for m in dir(live):
    if not m.startswith("_"):
        print(" ", m)
