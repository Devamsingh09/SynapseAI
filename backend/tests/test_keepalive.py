"""
Tests for the keep-alive visit (keepalive.py) against a fake server
(httpx.MockTransport): the exact request sequence, cookie handling, and that
the test chat is always deleted. No network.
"""
import asyncio
import json

import httpx

import keepalive


def make_transport(login_status=200, chat_body=None, chat_status=200):
    calls = []
    chat_body = chat_body if chat_body is not None else (
        'data: {"status": "using_tools"}\n\n'
        'data: {"token": "Hello"}\n\n'
        'data: {"token": "!"}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers.get("cookie")))
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"ok": True})
        if path == "/auth/login":
            body = json.loads(request.content)
            assert body == {"email": "me@example.com", "password": "pw"}
            if login_status != 200:
                return httpx.Response(login_status, json={"detail": "Incorrect email or password."})
            return httpx.Response(200, json={"id": 1}, headers={"set-cookie": "access_token=tok; Path=/; HttpOnly"})
        if path == "/thread/new":
            return httpx.Response(200, json={"thread_id": "t-123"})
        if path == "/chat/stream":
            assert json.loads(request.content) == {"thread_id": "t-123", "message": "hi", "voice": False}
            return httpx.Response(chat_status, text=chat_body, headers={"content-type": "text/event-stream"})
        if request.method == "DELETE" and path == "/thread/t-123":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    return httpx.MockTransport(handler), calls


def run(coro):
    return asyncio.run(coro)


def test_full_visit_logs_in_chats_and_deletes():
    transport, calls = make_transport()
    result = run(keepalive.run_once("https://space.example", "me@example.com", "pw", transport))
    assert [(m, p) for m, p, _ in calls] == [
        ("GET", "/health"),
        ("POST", "/auth/login"),
        ("POST", "/thread/new"),
        ("POST", "/chat/stream"),
        ("DELETE", "/thread/t-123"),
    ]
    # session cookie from login is sent on every later call
    assert all(c == "access_token=tok" for _, _, c in calls[2:])
    assert result == {"health": 200, "login": 200, "chat": "ok (6 chars)", "deleted": 200}


def test_without_credentials_only_health():
    transport, calls = make_transport()
    result = run(keepalive.run_once("https://space.example", None, None, transport))
    assert [(m, p) for m, p, _ in calls] == [("GET", "/health")]
    assert result["health"] == 200 and result["login"].startswith("skipped")


def test_wrong_login_still_visits_health_and_creates_nothing():
    transport, calls = make_transport(login_status=401)
    result = run(keepalive.run_once("https://space.example", "me@example.com", "pw", transport))
    assert [(m, p) for m, p, _ in calls] == [("GET", "/health"), ("POST", "/auth/login")]
    assert result["login"] == 401 and result["deleted"] is None


def test_chat_error_still_deletes_thread():
    transport, calls = make_transport(chat_body='data: {"error": "rate limited"}\n\n')
    result = run(keepalive.run_once("https://space.example", "me@example.com", "pw", transport))
    assert ("DELETE", "/thread/t-123") in [(m, p) for m, p, _ in calls]
    assert result["chat"].startswith("failed") and result["deleted"] == 200


def test_chat_http_error_still_deletes_thread():
    transport, calls = make_transport(chat_status=503, chat_body="busy")
    result = run(keepalive.run_once("https://space.example", "me@example.com", "pw", transport))
    assert result["chat"].startswith("failed") and result["deleted"] == 200


def test_unreachable_server_never_raises():
    def boom(request):
        raise httpx.ConnectError("no route")
    result = run(keepalive.run_once("https://space.example", "me@example.com", "pw", httpx.MockTransport(boom)))
    assert result["health"].startswith("failed") and result["login"] is None


def test_settings():
    assert keepalive.resolve_base_url({"SPACE_HOST": "me-app.hf.space"}) == "https://me-app.hf.space"
    assert keepalive.resolve_base_url({"KEEPALIVE_URL": "https://x.io/", "SPACE_HOST": "y"}) == "https://x.io"
    assert keepalive.resolve_base_url({}) is None
    assert keepalive.interval_seconds({}) == 7 * 3600
    assert keepalive.interval_seconds({"KEEPALIVE_INTERVAL_HOURS": "100"}) == 47 * 3600
    assert keepalive.interval_seconds({"KEEPALIVE_INTERVAL_HOURS": "junk"}) == 7 * 3600
    assert keepalive.is_enabled({}) and not keepalive.is_enabled({"KEEPALIVE_ENABLED": "false"})


def test_start_is_noop_locally():
    async def go():
        return keepalive.start({})
    assert run(go()) is None
