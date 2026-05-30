from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from langchain_core.messages import HumanMessage, AIMessage
import uuid
import json
import asyncio
import os

from chatbot_backend import chatbot, retrieve_all_threads, delete_thread_data, generate_summary
from voice_service import transcribe_audio, synthesize_speech, clean_text_for_speech

# ─────────────────────────────────────────────────────────────────────────────
# APP SETUP
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="Synapse AI API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────────────────────────────────────
# PYDANTIC MODELS
# ─────────────────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    thread_id: str
    message: str

class SummaryRequest(BaseModel):
    text: str

class SpeakRequest(BaseModel):
    text: str

# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "Synapse AI API is running 🧠"}


@app.post("/thread/new")
def new_thread():
    """Create a new thread ID."""
    tid = str(uuid.uuid4())
    return {"thread_id": tid}


@app.get("/threads")
def get_threads():
    """Return all saved thread IDs."""
    return {"threads": retrieve_all_threads()}


@app.get("/thread/{thread_id}/history")
def get_thread_history(thread_id: str):
    """Load full message history for a thread from LangGraph state."""
    try:
        state = chatbot.get_state(config={"configurable": {"thread_id": thread_id}})
        history = state.values.get("messages", [])

        messages = []
        for msg in history:
            if isinstance(msg, HumanMessage):
                messages.append({"role": "user", "content": msg.content})
            elif isinstance(msg, AIMessage) and msg.content:
                messages.append({"role": "assistant", "content": msg.content})

        user_msgs = [m for m in messages if m["role"] == "user"]
        title = generate_summary(user_msgs[0]["content"]) if user_msgs else "New Conversation"

        return {"thread_id": thread_id, "title": title, "messages": messages}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/thread/{thread_id}")
def delete_thread(thread_id: str):
    """Delete a thread and its data."""
    delete_thread_data(thread_id)
    return {"deleted": thread_id}


@app.post("/chat/summary")
def summarize(req: SummaryRequest):
    """Generate a short title from the first user message."""
    title = generate_summary(req.text)
    return {"title": title}


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    """
    Stream the assistant reply token-by-token using Server-Sent Events (SSE).
    The frontend reads this as a text/event-stream.
    """
    async def event_generator():
        try:
            loop = asyncio.get_event_loop()

            # ✅ FIX: Wrapped in asyncio.wait_for with 60s timeout
            # Prevents the chat from freezing silently if the LLM or
            # summarizer hangs (e.g. Groq rate limit, slow network)
            chunks = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: list(chatbot.stream(
                        {"messages": [HumanMessage(content=req.message)]},
                        config={"configurable": {"thread_id": req.thread_id}},
                        stream_mode="messages",
                    ))
                ),
                timeout=60
            )

            for chunk, metadata in chunks:
                if (
                    isinstance(chunk, AIMessage)
                    and chunk.content
                    and metadata.get("langgraph_node") == "chat_node"
                ):
                    data = json.dumps({"token": chunk.content})
                    yield f"data: {data}\n\n"
                    await asyncio.sleep(0)

            yield "data: [DONE]\n\n"

        except asyncio.TimeoutError:
            # ✅ FIX: Send a clean error to frontend instead of hanging forever
            yield f"data: {json.dumps({'error': 'Request timed out. Please try again.'})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/voice/status")
def voice_status():
    """Check whether voice endpoints are configured."""
    return {
        "stt": bool(os.getenv("GROQ_API_KEY")),
        "tts": True,
        "stt_model": os.getenv("STT_MODEL", "whisper-large-v3-turbo"),
        "tts_voice": os.getenv("TTS_VOICE", "en-US-AriaNeural"),
    }


@app.post("/voice/transcribe")
async def voice_transcribe(audio: UploadFile = File(...)):
    """Speech-to-text via Groq Whisper. Accepts webm/wav/mp3/ogg."""
    try:
        audio_bytes = await audio.read()
        if not audio_bytes:
            raise HTTPException(status_code=400, detail="Empty audio file")

        filename = audio.filename or "audio.webm"
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(
            None, lambda: transcribe_audio(audio_bytes, filename)
        )

        if not text:
            raise HTTPException(status_code=422, detail="Could not transcribe audio")

        return {"text": text}

    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/voice/speak")
async def voice_speak(req: SpeakRequest):
    """Text-to-speech via Edge TTS. Returns MP3 audio."""
    try:
        cleaned = clean_text_for_speech(req.text)
        if not cleaned:
            raise HTTPException(status_code=400, detail="No speakable text")

        audio_bytes = await synthesize_speech(cleaned)
        if not audio_bytes:
            raise HTTPException(status_code=500, detail="TTS produced no audio")

        return Response(content=audio_bytes, media_type="audio/mpeg")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))