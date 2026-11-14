"""
Tests for the in-app feedback form: input rules, the per-user rate limit,
the email body (escaping, reply-to) and the /feedback route. No network:
the Resend call is replaced with a stub.
"""
import pytest

import email_service
import feedback


# ── input rules ──────────────────────────────────────────────────────────────

def test_name_whitespace_and_newlines_collapse():
    assert feedback.normalize_name("  Asha \n  Rao\t") == "Asha Rao"


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_name_required(bad):
    with pytest.raises(ValueError, match="enter your name"):
        feedback.normalize_name(bad)


def test_name_too_long():
    with pytest.raises(ValueError, match="80 characters"):
        feedback.normalize_name("a" * 81)


def test_message_keeps_line_breaks_and_trims():
    assert feedback.normalize_message("  Great app!\r\nVoice is slow.  ") == "Great app!\nVoice is slow."


@pytest.mark.parametrize("bad", ["", "  ", "ok"])
def test_message_too_short(bad):
    with pytest.raises(ValueError, match="write a little feedback"):
        feedback.normalize_message(bad)


def test_message_too_long():
    feedback.normalize_message("x" * 1000)
    with pytest.raises(ValueError, match="1000 characters"):
        feedback.normalize_message("x" * 1001)


# ── rate limit ───────────────────────────────────────────────────────────────

def test_rate_limit_per_user_and_window():
    now = [0.0]
    rl = feedback.RateLimiter(limit=2, window_sec=60, clock=lambda: now[0])
    assert rl.allow(1) and rl.allow(1)
    assert not rl.allow(1)
    assert rl.allow(2)            # other users are unaffected
    now[0] = 60.0
    assert rl.allow(1)            # window slid past the first hits


def test_refund_returns_a_slot():
    rl = feedback.RateLimiter(limit=1, window_sec=60, clock=lambda: 0.0)
    assert rl.allow("u")
    rl.refund("u")
    assert rl.allow("u")


# ── email body ───────────────────────────────────────────────────────────────

def test_email_escapes_user_text_and_keeps_line_breaks():
    subject, body = feedback.build_email(
        "Eve <script>", "eve@example.com", "<b>hi</b>\nline two", user_id=7
    )
    assert subject == "SynapseAI feedback from Eve <script>"  # plain-text header
    assert "<script>" not in body and "&lt;script&gt;" in body
    assert "&lt;b&gt;hi&lt;/b&gt;<br>line two" in body
    assert "mailto:eve@example.com" in body
    assert "account #7" in body


def test_send_feedback_email_goes_to_creator_with_reply_to(monkeypatch):
    calls = []
    monkeypatch.setattr(email_service, "_send", lambda *a, **k: calls.append((a, k)) or True)
    monkeypatch.setattr(email_service, "FEEDBACK_TO_EMAIL", "owner@example.com")
    assert email_service.send_feedback_email("Asha", "asha@example.com", "Nice work", user_id=3)
    (to, subject, _html), kwargs = calls[0]
    assert to == "owner@example.com"
    assert subject == "SynapseAI feedback from Asha"
    assert kwargs == {"reply_to": "asha@example.com"}


# ── /feedback route ──────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    main = pytest.importorskip("main")
    from fastapi.testclient import TestClient

    import auth

    sent = []
    monkeypatch.setattr(
        email_service, "send_feedback_email",
        lambda name, email, message, user_id=None: sent.append((name, email, message, user_id)) or True,
    )
    monkeypatch.setattr(main, "_feedback_limiter", feedback.RateLimiter(limit=2, window_sec=3600))
    main.app.dependency_overrides[auth.get_current_user] = lambda: auth.User(
        id=42, email="session@example.com", is_verified=True
    )
    c = TestClient(main.app)
    c.sent = sent
    yield c
    main.app.dependency_overrides.clear()


def test_route_sends_with_session_email(client):
    r = client.post("/feedback", json={"name": " Asha ", "message": "Love the new chat page!"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert client.sent == [("Asha", "session@example.com", "Love the new chat page!", 42)]


def test_route_ignores_email_in_body(client):
    client.post("/feedback", json={"name": "A", "message": "hello there", "email": "spoof@evil.com"})
    assert client.sent[0][1] == "session@example.com"


def test_route_validation_error(client):
    r = client.post("/feedback", json={"name": "", "message": "hello there"})
    assert r.status_code == 422
    assert client.sent == []


def test_route_rate_limited(client):
    for _ in range(2):
        assert client.post("/feedback", json={"name": "A", "message": "hello there"}).status_code == 200
    r = client.post("/feedback", json={"name": "A", "message": "hello there"})
    assert r.status_code == 429


def test_route_send_failure_is_503_and_refunded(client, monkeypatch):
    import main

    monkeypatch.setattr(email_service, "send_feedback_email", lambda *a, **k: False)
    for _ in range(3):  # failures don't use up the limit of 2
        assert client.post("/feedback", json={"name": "A", "message": "hello there"}).status_code == 503


def test_route_requires_login():
    main = pytest.importorskip("main")
    from fastapi.testclient import TestClient

    r = TestClient(main.app).post("/feedback", json={"name": "A", "message": "hello there"})
    assert r.status_code == 401
