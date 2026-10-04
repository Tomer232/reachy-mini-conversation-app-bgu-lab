"""Compare voices for Hebrew quality on gemini-3.1-flash-live-preview."""
import sys, asyncio, base64, time
from pathlib import Path
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import numpy as np
import soundfile as sf
from google import genai
from google.genai import types

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "archive"
SAMPLE = OUT_DIR / "bench_input_he.wav"

def key():
    import os
    k = os.environ.get("GEMINI_API_KEY")
    if k: return k.strip()
    p = ROOT / ".gemini_key"
    if p.exists():
        return p.read_text().strip()
    return (ROOT.parent / "reachy-mini llm gemini token.txt").read_text().strip()

SYSTEM_PROMPT_HE = (
    "אתה Reachy Mini, רובוט שולחני קטן וידידותי. ענה תמיד בעברית. "
    "תגובות קצרות, משפט או שניים. היה חם וסקרן."
)
MODEL = "gemini-3.1-flash-live-preview"
VOICES = ["Aoede", "Kore", "Charon", "Puck", "Leda"]

async def try_voice(client, voice, audio16):
    cfg = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        system_instruction=SYSTEM_PROMPT_HE,
        speech_config=types.SpeechConfig(
            language_code="he-IL",
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
            ),
        ),
    )
    chunks = []
    asst = []
    t0 = time.perf_counter()
    first_t = None
    async with client.aio.live.connect(model=MODEL, config=cfg) as session:
        await session.send_realtime_input(
            audio=types.Blob(data=audio16.tobytes(), mime_type="audio/pcm;rate=16000")
        )
        await session.send_realtime_input(audio_stream_end=True)
        async for resp in session.receive():
            sc = resp.server_content
            if sc is None:
                continue
            if sc.model_turn and sc.model_turn.parts:
                for part in sc.model_turn.parts:
                    if part.inline_data and part.inline_data.data:
                        b = part.inline_data.data
                        if isinstance(b, str):
                            b = base64.b64decode(b)
                        if b:
                            if first_t is None:
                                first_t = time.perf_counter()
                            chunks.append(b)
            if sc.output_transcription and sc.output_transcription.text:
                asst.append(sc.output_transcription.text)
            if sc.turn_complete:
                break
    return b"".join(chunks), "".join(asst).strip(), (first_t - t0) if first_t else None, time.perf_counter()-t0

async def main():
    audio, sr = sf.read(str(SAMPLE), dtype="int16")
    assert sr == 16000
    client = genai.Client(api_key=key())
    for v in VOICES:
        try:
            audio_bytes, txt, fc, tot = await try_voice(client, v, audio)
            arr = np.frombuffer(audio_bytes, dtype=np.int16)
            out = OUT_DIR / f"voice_{v}.wav"
            sf.write(out, arr, 24000, subtype="PCM_16")
            print(f"{v:8s}  fc={fc:.2f}s tot={tot:.2f}s  {len(arr)/24000:.1f}s audio  saved {out.name}")
            print(f"           text: {txt!r}")
        except Exception as e:
            print(f"{v:8s}  ERROR: {e}")

asyncio.run(main())
