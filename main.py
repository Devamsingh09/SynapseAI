from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from langchain_core.messages import HumanMessage, AIMessage
import uuid
import json
import asyncio

from chatbot_backend import chatbot, retrieve_all_threads, delete_thread_data, generate_summary

# ─────────────────────────────────────────────────────────────────────────────
# APP SETUP
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="Synapse AI API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten to your frontend URL in production
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

            # LangGraph's .stream() is synchronous — run it in a thread pool
            # so it doesn't block the async event loop
            def run_stream():
                chunks = []
                for chunk, _ in chatbot.stream(
                    {"messages": [HumanMessage(content=req.message)]},
                    config={"configurable": {"thread_id": req.thread_id}},
                    stream_mode="messages",
                ):
                    if isinstance(chunk, AIMessage) and chunk.content:
                        chunks.append(chunk.content)
                return chunks

            # Stream token by token
            for chunk, _ in await loop.run_in_executor(
                None,
                lambda: list(chatbot.stream(
                    {"messages": [HumanMessage(content=req.message)]},
                    config={"configurable": {"thread_id": req.thread_id}},
                    stream_mode="messages",
                ))
            ):
                if isinstance(chunk, AIMessage) and chunk.content:
                    data = json.dumps({"token": chunk.content})
                    yield f"data: {data}\n\n"
                    await asyncio.sleep(0)   # yield control back to event loop

            yield "data: [DONE]\n\n"

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