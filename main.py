# main.py
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import json
import uuid
import asyncio
import time

from sse_starlette.sse import EventSourceResponse
from chatbot_backend import chatbot, retrieve_all_threads, delete_thread_memory
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, BaseMessage

app = FastAPI(title="Synapse AI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- UTILITIES ---

def new_thread_id() -> str:
    return str(uuid.uuid4())

def message_to_dict(m: BaseMessage) -> Dict[str, Any]:
    role = "user" if isinstance(m, HumanMessage) else "assistant" if isinstance(m, AIMessage) else "system"
    return {"role": role, "content": getattr(m, "content", "")}

class CreateThreadResponse(BaseModel):
    thread_id: str

class ChatRequest(BaseModel):
    message: str
    thread_id: Optional[str] = None

# --- ROUTES ---

@app.post("/threads", response_model=CreateThreadResponse)
def create_thread():
    return {"thread_id": new_thread_id()}

@app.get("/threads")
def list_threads():
    threads = retrieve_all_threads() or []
    items = [{"thread_id": tid, "title": tid[:8]} for tid in threads]
    return {"threads": items}

@app.delete("/threads/{thread_id}")
def delete_thread(thread_id: str):
    delete_thread_memory(thread_id)
    return {"ok": True}

@app.get("/threads/{thread_id}")
def load_thread(thread_id: str):
    state = chatbot.get_state(config={"configurable": {"thread_id": thread_id}})
    msgs = state.values.get("messages", []) if state and state.values else []
    return {"thread_id": thread_id, "messages": [message_to_dict(m) for m in msgs]}

# --- STREAMING ENDPOINT ---

@app.get("/chat/stream")
async def chat_stream_get(
    request: Request,
    message: str = Query(...),
    thread_id: str = Query(...),
    request_id: str = Query(...) # Kept to satisfy frontend, but ignored logic-wise
):
    async def event_generator():
        # 1. Send immediate ping to confirm connection
        yield "event: ping\ndata: start\n\n"

        q = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def run_bot():
            try:
                print(f"DEBUG: Generating for {thread_id}...")
                # Stream the chatbot response
                for chunk, meta in chatbot.stream(
                    {"messages": [HumanMessage(content=message)]},
                    config={"configurable": {"thread_id": thread_id}},
                    stream_mode="messages",
                ):
                    if isinstance(chunk, AIMessage) and chunk.content:
                        asyncio.run_coroutine_threadsafe(q.put(("token", chunk.content)), loop)
                
                asyncio.run_coroutine_threadsafe(q.put(("done", "")), loop)
            except Exception as e:
                print(f"ERROR in bot: {e}")
                asyncio.run_coroutine_threadsafe(q.put(("error", str(e))), loop)

        # Run bot in background thread
        import threading
        t = threading.Thread(target=run_bot, daemon=True)
        t.start()

        while True:
            # Check for client disconnect
            if await request.is_disconnected():
                print("DEBUG: Client disconnected")
                break

            try:
                # Wait for next token
                data = await asyncio.wait_for(q.get(), timeout=1.0)
                event_type, payload = data
                
                if event_type == "token":
                    yield f"event: message\ndata: {json.dumps({'delta': payload})}\n\n"
                elif event_type == "done":
                    yield "event: done\ndata: {}\n\n"
                    break
                elif event_type == "error":
                    yield f"event: error\ndata: {json.dumps({'error': payload})}\n\n"
                    break

            except asyncio.TimeoutError:
                # Send keep-alive ping if model is slow
                yield "event: ping\ndata: keepalive\n\n"

    return EventSourceResponse(event_generator(), media_type="text/event-stream")