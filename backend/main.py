import re
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Response, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator
from langchain_core.messages import HumanMessage, AIMessage
import uuid
import json
import asyncio
import threading

import auth
import email_service
from chatbot_backend import (
    chatbot,
    retrieve_all_threads,
    delete_thread_data,
    generate_summary,
    iter_chat_stream,
    get_thread_lock,
    GROQ_API_KEY,
)
from voice_service import VOICE_OPTIONS, DEFAULT_VOICE, synthesize_speech
from file_service import process_upload
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

# Auth uses an httpOnly session cookie, which browsers refuse to send/accept
# cross-origin when the server allows "*" with credentials — so this must be
# an explicit origin list rather than a wildcard once login exists.
_FRONTEND_ORIGINS = [o.strip() for o in auth.FRONTEND_ORIGIN.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_FRONTEND_ORIGINS,
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


class StopRequest(BaseModel):
    thread_id: str


class TtsRequest(BaseModel):
    text: str
    voice: Optional[str] = None


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SignupRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _valid_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not _EMAIL_RE.match(v):
            raise ValueError("Enter a valid email address.")
        return v

    @field_validator("password")
    @classmethod
    def _valid_password(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters.")
        return v


class LoginRequest(BaseModel):
    email: str
    password: str


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _valid_password(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters.")
        return v


class ResendVerificationRequest(BaseModel):
    email: str


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


# Most recent /chat/stream request per thread → its stop Event, for /chat/stop.
_stop_events: dict[str, threading.Event] = {}


def _graph_worker(
    message: str,
    thread_id: str,
    voice: bool,
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue,
    stop_event: threading.Event,
) -> None:
    """Runs in thread pool; pushes stream events onto the async queue."""
    try:
        for event in iter_chat_stream(message, thread_id, voice=voice, stop_event=stop_event):
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


# ─────────────────────────────────────────────────────────────────────────────
# AUTH ROUTES
# ─────────────────────────────────────────────────────────────────────────────


def _user_payload(user) -> dict:
    return {"id": user.id, "email": user.email, "is_verified": user.is_verified}


@app.post("/auth/signup", status_code=201)
async def signup(req: SignupRequest):
    """Create an account and email a verification link. Login is blocked
    until the email is verified (full verify+reset flow, not basic auth)."""
    try:
        async with io_slot():
            user = await run_in_pool(auth.create_user, req.email, req.password, timeout=IO_TIMEOUT_SEC)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    token = auth.create_verification_token(user.id)
    email_sent = email_service.send_verification_email(user.email, token)
    return {
        "message": "Account created. Check your email to verify your account before logging in.",
        "email_sent": email_sent,
    }


@app.get("/auth/verify-email")
async def verify_email(token: str):
    """Called by the link in the verification email."""
    user_id = auth.consume_email_token(token, "verify_email")
    if user_id is None:
        raise HTTPException(status_code=400, detail="This verification link is invalid or has expired.")
    auth.mark_verified(user_id)
    return {"message": "Email verified — you can now log in."}


@app.post("/auth/resend-verification")
async def resend_verification(req: ResendVerificationRequest):
    """Always returns success regardless of whether the email exists/is
    already verified — avoids leaking which emails have accounts."""
    user = auth.get_user_by_email(req.email)
    if user and not user["is_verified"]:
        token = auth.create_verification_token(user["id"])
        email_service.send_verification_email(user["email"], token)
    return {"message": "If that email has a pending account, a new verification link has been sent."}


@app.post("/auth/login")
async def login(req: LoginRequest, response: Response):
    user = auth.get_user_by_email(req.email)
    if not user or not auth.verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Incorrect email or password.")
    if not user["is_verified"]:
        raise HTTPException(status_code=403, detail="Please verify your email before logging in.")

    token = auth.create_access_token(user["id"], user["email"])
    auth.set_session_cookie(response, token)
    return {"id": user["id"], "email": user["email"], "is_verified": True}


@app.post("/auth/logout")
async def logout(response: Response):
    auth.clear_session_cookie(response)
    return {"message": "Logged out."}


@app.get("/auth/me")
async def me(current_user: auth.User = Depends(auth.get_current_user)):
    return _user_payload(current_user)


@app.post("/auth/forgot-password")
async def forgot_password(req: ForgotPasswordRequest):
    """Always returns success — never reveals whether the email has an account."""
    user = auth.get_user_by_email(req.email)
    if user:
        token = auth.create_reset_token(user["id"])
        email_service.send_password_reset_email(user["email"], token)
    return {"message": "If that email has an account, a password reset link has been sent."}


@app.post("/auth/reset-password")
async def reset_password(req: ResetPasswordRequest):
    user_id = auth.consume_email_token(req.token, "reset_password")
    if user_id is None:
        raise HTTPException(status_code=400, detail="This reset link is invalid or has expired.")
    auth.update_password(user_id, req.new_password)
    return {"message": "Password updated — you can now log in."}


# ─────────────────────────────────────────────────────────────────────────────
# THREAD / CHAT ROUTES (require login)
# ─────────────────────────────────────────────────────────────────────────────


@app.post("/thread/new")
async def new_thread(current_user: auth.User = Depends(auth.get_current_user)):
    """Create a new thread ID, owned by the current user."""
    tid = str(uuid.uuid4())
    auth.claim_thread(tid, current_user.id)
    return {"thread_id": tid}


@app.get("/threads")
async def get_threads(current_user: auth.User = Depends(auth.get_current_user)):
    """Return this user's thread IDs only."""
    async with io_slot():
        all_threads = await run_in_pool(retrieve_all_threads, timeout=IO_TIMEOUT_SEC)
    owned = auth.list_user_thread_ids(current_user.id)
    return {"threads": [t for t in all_threads if t in owned]}


@app.get("/thread/{thread_id}/history")
async def get_thread_history(thread_id: str, current_user: auth.User = Depends(auth.get_current_user)):
    """Load full message history for a thread from LangGraph state."""
    auth.require_thread_owner(thread_id, current_user.id)
    try:
        async with io_slot():
            return await run_in_pool(_load_thread_history, thread_id, timeout=IO_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="History request timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/thread/{thread_id}")
async def delete_thread(thread_id: str, current_user: auth.User = Depends(auth.get_current_user)):
    """Delete a thread and its data."""
    auth.require_thread_owner(thread_id, current_user.id)
    async with io_slot():
        result = await run_in_pool(_delete_thread, thread_id, timeout=IO_TIMEOUT_SEC)
    auth.delete_thread_ownership(thread_id)
    return result


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


@app.get("/voice/options")
async def voice_options():
    """Curated voice list for the UI picker (edge-tts voices + browser default)."""
    return {"voices": VOICE_OPTIONS, "default": DEFAULT_VOICE}


@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    question: str = Form(""),
    current_user: auth.User = Depends(auth.get_current_user),
):
    """Extract text (PDF/DOCX) or describe an image (Groq vision). Returns
    content for the frontend to fold into the next chat message — doesn't
    touch LangGraph state directly."""
    data = await file.read()
    try:
        async with io_slot():
            kind, result = await run_in_pool(
                process_upload,
                file.filename or "upload",
                file.content_type or "",
                data,
                question,
                timeout=IO_TIMEOUT_SEC,
            )
            print(f"[upload ok] {file.filename} -> kind={kind}, content_preview={result[:200]!r}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"[upload error] {file.filename}: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="Couldn't process that file. Please try again.")

    return {"kind": kind, "filename": file.filename, "content": result}


@app.post("/chat/summary")
async def summarize(req: SummaryRequest, current_user: auth.User = Depends(auth.get_current_user)):
    """Generate a short title from the first user message."""
    async with io_slot():
        title = await run_in_pool(generate_summary, req.text, timeout=IO_TIMEOUT_SEC)
    return {"title": title}


@app.post("/chat/stop")
async def chat_stop(req: StopRequest, current_user: auth.User = Depends(auth.get_current_user)):
    """Stop the answer currently being generated on this thread. The text
    produced so far is kept as the assistant's reply. Idempotent."""
    if auth.get_thread_owner(req.thread_id) != current_user.id:
        raise HTTPException(status_code=404, detail="Thread not found")
    event = _stop_events.get(req.thread_id)
    if event:
        event.set()
    return {"stopped": bool(event)}


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest, current_user: auth.User = Depends(auth.get_current_user)):
    """
    Stream the assistant reply using SSE.
    Up to MAX_CONCURRENT_REQUESTS (default 50) streams can run in parallel.
    """
    # The frontend generates the very first thread_id client-side (before
    # ever calling POST /thread/new), so ownership may not be claimed yet —
    # claim it here on first use rather than requiring pre-registration.
    # Once claimed, only the owner may keep chatting on that thread_id.
    owner_id = auth.get_thread_owner(req.thread_id)
    if owner_id is None:
        auth.claim_thread(req.thread_id, current_user.id)
    elif owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Thread not found")

    stop_event = threading.Event()
    _stop_events[req.thread_id] = stop_event

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
                stop_event,
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

            except (asyncio.CancelledError, GeneratorExit):
                # Client went away (Stop pressed, tab closed): stop the graph
                # too instead of letting it run to completion unseen.
                stop_event.set()
                raise
            finally:
                await worker
                if _stop_events.get(req.thread_id) is stop_event:
                    del _stop_events[req.thread_id]

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
