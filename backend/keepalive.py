"""
Keep-alive: stop the free Hugging Face Space from going to sleep.

Free Spaces sleep after 48 hours without visitors. This runs inside the
backend itself and, every KEEPALIVE_INTERVAL_HOURS (default 7), goes out
through the Space's PUBLIC URL (so Hugging Face sees a real visit):

  1. GET  /health                      — always; this alone counts as a visit
  2. POST /auth/login                  — with KEEPALIVE_EMAIL / KEEPALIVE_PASSWORD
  3. POST /thread/new
  4. POST /chat/stream  "hi"           — reads the reply to the end
  5. DELETE /thread/{id}               — always, even if the chat failed

If the account can't log in (for example the database was reset by a
rebuild), steps 2–5 are skipped and logged, but step 1 still keeps the
Space awake.

Login: DEFAULT_EMAIL / DEFAULT_PASSWORD below (the owner chose to keep them
in code; note this repo is public). Space secrets with the same names
override them.

Settings (Space → Settings → Variables and secrets):
  KEEPALIVE_EMAIL, KEEPALIVE_PASSWORD   optional overrides of the defaults
  KEEPALIVE_INTERVAL_HOURS              default 7
  KEEPALIVE_ENABLED                     "false" turns it off
  KEEPALIVE_URL                         default https://$SPACE_HOST (set by HF)
"""
import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

DEFAULT_INTERVAL_HOURS = 7.0
FIRST_RUN_DELAY_SEC = 120          # let the app finish starting first
REQUEST_TIMEOUT_SEC = 60.0
CHAT_TIMEOUT_SEC = 180.0
MESSAGE = "hi"

DEFAULT_EMAIL = "devamsingh0009@gmail.com"
DEFAULT_PASSWORD = "SinghDevam09"


def _log(msg: str) -> None:
    print(f"[keepalive {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


def resolve_base_url(env=os.environ) -> Optional[str]:
    url = (env.get("KEEPALIVE_URL") or "").strip()
    if not url and env.get("SPACE_HOST"):      # set automatically on HF Spaces
        url = f"https://{env['SPACE_HOST'].strip()}"
    return url.rstrip("/") or None


def interval_seconds(env=os.environ) -> float:
    try:
        hours = float(env.get("KEEPALIVE_INTERVAL_HOURS") or DEFAULT_INTERVAL_HOURS)
    except ValueError:
        hours = DEFAULT_INTERVAL_HOURS
    # Never more often than every 10 minutes, never past the 48 h sleep timer.
    return min(max(hours, 1 / 6), 47.0) * 3600


def is_enabled(env=os.environ) -> bool:
    return (env.get("KEEPALIVE_ENABLED") or "true").strip().lower() not in ("false", "0", "no", "off")


async def _read_stream(resp: httpx.Response) -> str:
    """Read the SSE reply to the end; return the answer text."""
    text = ""
    async for line in resp.aiter_lines():
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if payload == "[DONE]":
            break
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        if obj.get("error"):
            raise RuntimeError(f"chat error: {obj['error']}")
        text += obj.get("token") or ""
    return text


async def run_once(base_url: str, email: Optional[str], password: Optional[str],
                   transport: Optional[httpx.AsyncBaseTransport] = None) -> dict:
    """One keep-alive visit. Never raises; returns what happened."""
    result = {"health": None, "login": None, "chat": None, "deleted": None}
    started = time.monotonic()
    async with httpx.AsyncClient(base_url=base_url, timeout=REQUEST_TIMEOUT_SEC,
                                 transport=transport, follow_redirects=True) as client:
        try:
            r = await client.get("/health")
            result["health"] = r.status_code
        except httpx.HTTPError as exc:
            result["health"] = f"failed: {type(exc).__name__}"
            _log(f"health check failed: {exc!r}")
            return result

        if not (email and password):
            result["login"] = "skipped (no KEEPALIVE_EMAIL / KEEPALIVE_PASSWORD)"
            _log(f"visited /health ({result['health']}); chat skipped: no credentials set")
            return result

        thread_id = None
        try:
            r = await client.post("/auth/login", json={"email": email, "password": password})
            result["login"] = r.status_code
            if r.status_code != 200:
                _log(f"login failed ({r.status_code}): {r.text[:200]} — /health visit still keeps the Space awake")
                return result

            r = await client.post("/thread/new")
            r.raise_for_status()
            thread_id = r.json()["thread_id"]

            async with client.stream("POST", "/chat/stream",
                                     json={"thread_id": thread_id, "message": MESSAGE, "voice": False},
                                     timeout=CHAT_TIMEOUT_SEC) as resp:
                resp.raise_for_status()
                answer = await _read_stream(resp)
            result["chat"] = f"ok ({len(answer)} chars)"
        except Exception as exc:  # keep going to the delete step
            result["chat"] = result["chat"] or f"failed: {type(exc).__name__}: {exc}"
        finally:
            if thread_id:
                try:
                    r = await client.delete(f"/thread/{thread_id}")
                    result["deleted"] = r.status_code
                except httpx.HTTPError as exc:
                    result["deleted"] = f"failed: {type(exc).__name__}"

    _log(f"done in {time.monotonic() - started:.1f}s: {result}")
    return result


async def keepalive_loop(env=os.environ) -> None:
    base = resolve_base_url(env)
    every = interval_seconds(env)
    _log(f"started: visiting {base} every {every / 3600:g} h (first visit in {FIRST_RUN_DELAY_SEC}s)")
    await asyncio.sleep(FIRST_RUN_DELAY_SEC)
    while True:
        try:
            await run_once(base,
                           env.get("KEEPALIVE_EMAIL") or DEFAULT_EMAIL,
                           env.get("KEEPALIVE_PASSWORD") or DEFAULT_PASSWORD)
        except Exception as exc:  # never let the loop die
            _log(f"unexpected error: {exc!r}")
        await asyncio.sleep(every)


def start(env=os.environ) -> Optional[asyncio.Task]:
    """Start the loop from the app's lifespan. Returns None when disabled."""
    if not is_enabled(env):
        _log("disabled (KEEPALIVE_ENABLED=false)")
        return None
    if not resolve_base_url(env):
        _log("not started: no SPACE_HOST or KEEPALIVE_URL (normal for local dev)")
        return None
    return asyncio.create_task(keepalive_loop(env))
