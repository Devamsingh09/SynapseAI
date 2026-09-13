"""
Text-to-speech via Microsoft Edge neural voices (edge-tts).
Free, no API key — suitable for local dev and HF Spaces.
"""
import io
import re
from typing import Optional

import edge_tts

DEFAULT_VOICE = "en-US-AriaNeural"

# Curated voices for UI picker (all free via edge-tts)
VOICE_OPTIONS = [
    {"id": "en-US-AriaNeural", "label": "Aria (US, female)"},
    {"id": "en-US-GuyNeural", "label": "Guy (US, male)"},
    {"id": "en-GB-SoniaNeural", "label": "Sonia (UK, female)"},
    {"id": "en-IN-NeerjaNeural", "label": "Neerja (India, female)"},
    {"id": "en-IN-PrabhatNeural", "label": "Prabhat (India, male)"},
    {"id": "hi-IN-SwaraNeural", "label": "Swara (Hindi, female)"},
]

# Hard cap per TTS request (Edge TTS handles long text but we chunk on client too)
MAX_TTS_CHARS = 500


def strip_for_speech(text: str) -> str:
    """Remove markdown/code so TTS sounds natural."""
    if not text:
        return ""
    t = text
    t = re.sub(r"```[\s\S]*?```", " ", t)
    t = re.sub(r"`([^`]+)`", r"\1", t)
    t = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"#{1,6}\s*", "", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)
    t = re.sub(r"\*([^*]+)\*", r"\1", t)
    t = re.sub(r"^[-*+]\s+", "", t, flags=re.MULTILINE)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:MAX_TTS_CHARS]


async def synthesize_speech(text: str, voice: Optional[str] = None) -> bytes:
    """Return MP3 bytes for the given text."""
    clean = strip_for_speech(text)
    if not clean:
        raise ValueError("No speakable text")

    voice_id = voice or DEFAULT_VOICE
    valid_ids = {v["id"] for v in VOICE_OPTIONS}
    if voice_id not in valid_ids:
        voice_id = DEFAULT_VOICE

    communicate = edge_tts.Communicate(clean, voice_id, rate="+12%")
    buffer = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buffer.write(chunk["data"])
    data = buffer.getvalue()
    if not data:
        raise RuntimeError("TTS produced empty audio")
    return data
