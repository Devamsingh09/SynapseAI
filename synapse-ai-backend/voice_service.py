import os
import re
import asyncio

import edge_tts
from groq import Groq

# Fastest Groq STT model (216x realtime on Groq LPU)
STT_MODEL = os.getenv("STT_MODEL", "whisper-large-v3-turbo")
# Edge TTS voice + faster speech rate for backend fallback
TTS_VOICE = os.getenv("TTS_VOICE", "en-US-JennyNeural")
TTS_RATE = os.getenv("TTS_RATE", "+12%")


def _groq_client() -> Groq:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise ValueError("GROQ_API_KEY is not configured")
    return Groq(api_key=api_key)


def clean_text_for_speech(text: str) -> str:
    """Strip markdown/code so TTS reads natural spoken language."""
    if not text:
        return ""

    cleaned = text
    cleaned = re.sub(r"```[\s\S]*?```", " ", cleaned)
    cleaned = re.sub(r"`[^`]+`", " ", cleaned)
    cleaned = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", cleaned)
    cleaned = re.sub(r"\[[^\]]+\]\([^)]+\)", " ", cleaned)
    cleaned = re.sub(r"https?://\S+", "link", cleaned)
    cleaned = re.sub(r"[#*_~>|]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def transcribe_audio(audio_bytes: bytes, filename: str = "audio.webm") -> str:
    """Transcribe with Groq Whisper Turbo — fastest cloud STT fallback."""
    client = _groq_client()
    result = client.audio.transcriptions.create(
        file=(filename, audio_bytes),
        model=STT_MODEL,
        response_format="text",
        language="en",
        temperature=0,
    )
    text = result if isinstance(result, str) else getattr(result, "text", str(result))
    return text.strip()


async def synthesize_speech(text: str) -> bytes:
    """Edge TTS fallback — used only when browser TTS unavailable."""
    cleaned = clean_text_for_speech(text)
    if not cleaned:
        return b""

    communicate = edge_tts.Communicate(cleaned, TTS_VOICE, rate=TTS_RATE)
    audio = b""
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio += chunk["data"]
    return audio


def synthesize_speech_sync(text: str) -> bytes:
    return asyncio.run(synthesize_speech(text))
