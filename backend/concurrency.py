"""
Async concurrency controls for Hugging Face Spaces (single-process, many clients).

- Semaphores cap in-flight work (default 50 concurrent chat streams).
- ThreadPoolExecutor runs sync LangGraph / LLM calls without blocking the event loop.
"""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial
from typing import Callable, Optional, TypeVar

T = TypeVar("T")

MAX_CONCURRENT_REQUESTS = int(os.getenv("MAX_CONCURRENT_REQUESTS", "50"))
CHAT_STREAM_TIMEOUT_SEC = int(os.getenv("CHAT_STREAM_TIMEOUT_SEC", "120"))
IO_TIMEOUT_SEC = int(os.getenv("IO_TIMEOUT_SEC", "60"))

# Admission control — up to N chat streams / heavy jobs at once
chat_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

# Lighter endpoints (history, summary, thread list)
io_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

executor = ThreadPoolExecutor(
    max_workers=MAX_CONCURRENT_REQUESTS,
    thread_name_prefix="synapse-worker",
)

_active_chat_streams = 0
_active_chat_lock = asyncio.Lock()


async def track_chat_stream_started() -> None:
    global _active_chat_streams
    async with _active_chat_lock:
        _active_chat_streams += 1


async def track_chat_stream_finished() -> None:
    global _active_chat_streams
    async with _active_chat_lock:
        _active_chat_streams = max(0, _active_chat_streams - 1)


async def get_concurrency_stats() -> dict:
    async with _active_chat_lock:
        active = _active_chat_streams
    return {
        "max_concurrent_requests": MAX_CONCURRENT_REQUESTS,
        "active_chat_streams": active,
        "chat_slots_available": max(0, MAX_CONCURRENT_REQUESTS - active),
        "chat_stream_timeout_sec": CHAT_STREAM_TIMEOUT_SEC,
    }


@asynccontextmanager
async def chat_slot():
    """Hold one chat concurrency slot for the full lifetime of a stream."""
    await chat_semaphore.acquire()
    await track_chat_stream_started()
    try:
        yield
    finally:
        await track_chat_stream_finished()
        chat_semaphore.release()


@asynccontextmanager
async def io_slot():
    """Hold one slot for short blocking I/O (DB read, title generation)."""
    await io_semaphore.acquire()
    try:
        yield
    finally:
        io_semaphore.release()


async def run_in_pool(fn: Callable[..., T], *args, timeout: Optional[int] = None, **kwargs) -> T:
    """Run a sync callable in the shared thread pool."""
    loop = asyncio.get_running_loop()
    bound = partial(fn, *args, **kwargs)
    coro = loop.run_in_executor(executor, bound)
    if timeout is not None:
        return await asyncio.wait_for(coro, timeout=timeout)
    return await coro


def shutdown_pool() -> None:
    executor.shutdown(wait=False, cancel_futures=True)
