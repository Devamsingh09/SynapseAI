from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from langchain_core.messages import HumanMessage, AIMessage
import uuid
import json
import asyncio

from chatbot_backend import (
    chatbot,
    retrieve_all_threads,
    delete_thread_data,
    generate_summary,
    iter_chat_stream,
    get_thread_lock,
    GROQ_API_KEY,
)
from voice_service import VOICE_OPTIONS, synthesize_speech
from concurrency import (
    CHAT_STREAM_TIMEOUT_SEC,
    IO_TIMEOUT_SEC,
    chat_slot,
    io_slot,
    executor,
    get_concurrency_stats,
    run_in_pool,
    shutdown_pool,
)

# ─────────────────────────────────────────────────────────────────────────────
# APP SETUP
# ─────────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not GROQ_API_KEY:
        print("WARNING: GROQ_API_KEY is missing — set it in synapse-ai-backend/.env")
    elif not GROQ_API_KEY.startswith("gsk_"):
        print("WARNING: GROQ_API_KEY format looks wrong — copy a fresh key from console.groq.com")
    yield
    shutdown_pool()


app = FastAPI(title="Synapse AI API", version="1.1.0", lifespan=lifespan)

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
    voice: bool = False


class SummaryRequest(BaseModel):
    text: str


class TtsRequest(BaseModel):
    text: str
    voice: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# ASYNC HELPERS
# ─────────────────────────────────────────────────────────────────────────────


def _load_thread_history(thread_id: str) -> dict:
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


def _delete_thread(thread_id: str) -> dict:
    with get_thread_lock(thread_id):
        delete_thread_data(thread_id)
    return {"deleted": thread_id}


def _graph_worker(
    message: str,
    thread_id: str,
    voice: bool,
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue,
) -> None:
    """Runs in thread pool; pushes stream events onto the async queue."""
    try:
        for event in iter_chat_stream(message, thread_id, voice=voice):
            loop.call_soon_threadsafe(queue.put_nowait, event)
    except Exception as exc:
        loop.call_soon_threadsafe(queue.put_nowait, ("error", str(exc)))


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/")
async def root():
    stats = await get_concurrency_stats()
    return {
        "status": "Synapse AI API is running 🧠",
        "async": True,
        **stats,
    }


@app.get("/health")
async def health():
    stats = await get_concurrency_stats()
    groq_ok = bool(GROQ_API_KEY and GROQ_API_KEY.startswith("gsk_"))
    return {
        "ok": groq_ok,
        "groq_configured": groq_ok,
        **stats,
    }


@app.post("/thread/new")
async def new_thread():
    """Create a new thread ID."""
    return {"thread_id": str(uuid.uuid4())}


@app.get("/threads")
async def get_threads():
    """Return all saved thread IDs."""
    async with io_slot():
        threads = await run_in_pool(retrieve_all_threads, timeout=IO_TIMEOUT_SEC)
    return {"threads": threads}


@app.get("/thread/{thread_id}/history")
async def get_thread_history(thread_id: str):
    """Load full message history for a thread from LangGraph state."""
    try:
        async with io_slot():
            return await run_in_pool(_load_thread_history, thread_id, timeout=IO_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="History request timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/thread/{thread_id}")
async def delete_thread(thread_id: str):
    """Delete a thread and its data."""
    async with io_slot():
        return await run_in_pool(_delete_thread, thread_id, timeout=IO_TIMEOUT_SEC)


@app.get("/voice/config")
async def voice_config():
    """Voice capabilities — STT/TTS run in the browser (Web Speech API)."""
    return {
        "stt": "browser",
        "tts": "browser",
        "note": "Speech synthesis uses the browser voice engine (Chrome/Edge). No server TTS.",
    }


@app.post("/voice/tts")
async def voice_tts(req: TtsRequest):
    """Synthesize speech (MP3) via Edge TTS — free neural voices."""
    try:
        audio = await synthesize_speech(req.text, req.voice)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"TTS failed: {e}")

    return Response(content=audio, media_type="audio/mpeg")


@app.post("/chat/summary")
async def summarize(req: SummaryRequest):
    """Generate a short title from the first user message."""
    async with io_slot():
        title = await run_in_pool(generate_summary, req.text, timeout=IO_TIMEOUT_SEC)
    return {"title": title}


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    """
    Stream the assistant reply using SSE.
    Up to MAX_CONCURRENT_REQUESTS (default 50) streams can run in parallel.
    """

    async def event_generator():
        async with chat_slot():
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue = asyncio.Queue()
            worker = loop.run_in_executor(
                executor,
                _graph_worker,
                req.message,
                req.thread_id,
                req.voice,
                loop,
                queue,
            )

            try:
                while True:
                    try:
                        kind, payload = await asyncio.wait_for(
                            queue.get(),
                            timeout=CHAT_STREAM_TIMEOUT_SEC,
                        )
                    except asyncio.TimeoutError:
                        yield f"data: {json.dumps({'error': 'Request timed out. Please try again.'})}\n\n"
                        break

                    if kind == "done":
                        yield "data: [DONE]\n\n"
                        break

                    if kind == "error":
                        yield f"data: {json.dumps({'error': payload})}\n\n"
                        break

                    if kind == "status":
                        yield f"data: {json.dumps({'status': payload})}\n\n"
                        continue

                    if kind == "token":
                        yield f"data: {json.dumps({'token': payload})}\n\n"

            finally:
                await worker

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
