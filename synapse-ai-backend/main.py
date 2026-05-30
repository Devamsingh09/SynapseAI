from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from langchain_core.messages import HumanMessage, AIMessage
import uuid
import json
import asyncio
import os

from chatbot_backend import (
    chatbot,
    retrieve_all_threads,
    delete_thread_data,
    generate_summary,
    extract_ai_text,
    get_latest_assistant_reply,
)
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
            elif isinstance(msg, AIMessage):
                text = extract_ai_text(msg.content)
                if text:
                    messages.append({"role": "assistant", "content": text})

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
    Runs the full LangGraph flow (tools + memory) — same path as text chat.
    """
    async def event_generator():
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()
        streamed_text = []

        def graph_worker():
            try:
                for chunk, metadata in chatbot.stream(
                    {"messages": [HumanMessage(content=req.message)]},
                    config={"configurable": {"thread_id": req.thread_id}},
                    stream_mode="messages",
                ):
                    loop.call_soon_threadsafe(
                        queue.put_nowait, ("chunk", chunk, metadata)
                    )
                loop.call_soon_threadsafe(queue.put_nowait, ("done", None, None))
            except Exception as exc:
                loop.call_soon_threadsafe(queue.put_nowait, ("error", str(exc), None))

        worker = loop.run_in_executor(None, graph_worker)

        try:
            while True:
                kind, chunk, metadata = await asyncio.wait_for(queue.get(), timeout=120)

                if kind == "done":
                    break
                if kind == "error":
                    yield f"data: {json.dumps({'error': chunk})}\n\n"
                    return

                node = metadata.get("langgraph_node") if metadata else None

                if node == "tools":
                    yield f"data: {json.dumps({'status': 'using_tools'})}\n\n"
                    continue

                if isinstance(chunk, AIMessage) and node == "chat_node":
                    text = extract_ai_text(chunk.content)
                    if text:
                        streamed_text.append(text)
                        yield f"data: {json.dumps({'token': text})}\n\n"

            await worker

            # Fallback: post-tool replies sometimes arrive as one block; ensure we send them
            if not streamed_text:
                final = get_latest_assistant_reply(req.thread_id)
                if final:
                    yield f"data: {json.dumps({'token': final})}\n\n"

            yield "data: [DONE]\n\n"

        except asyncio.TimeoutError:
            yield f"data: {json.dumps({'error': 'Request timed out (tools may still be running). Please try again.'})}\n\n"

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
        "stt_engine": "groq-whisper-turbo",
        "stt_model": os.getenv("STT_MODEL", "whisper-large-v3-turbo"),
        "tts_engine": "browser-primary-edge-fallback",
        "tts_voice": os.getenv("TTS_VOICE", "en-US-JennyNeural"),
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