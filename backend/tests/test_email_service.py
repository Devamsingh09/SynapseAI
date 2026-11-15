"""
email_service sender order: Gmail Apps Script first, Resend as fallback.
No network: httpx.post and resend are faked.
"""
import types

import httpx
import pytest

import email_service


class FakeResponse:
    def __init__(self, data=None, text=None):
        self._data, self._text = data, text

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


@pytest.fixture
def script(monkeypatch):
    calls = []
    state = {"response": FakeResponse({"ok": True, "remaining": 99})}

    def fake_post(url, json=None, timeout=None, follow_redirects=None):
        calls.append({"url": url, "json": json, "follow_redirects": follow_redirects})
        if isinstance(state["response"], Exception):
            raise state["response"]
        return state["response"]

    monkeypatch.setattr(email_service.httpx, "post", fake_post)
    monkeypatch.setattr(email_service, "GMAIL_SCRIPT_URL", "https://script.google.com/macros/s/x/exec")
    monkeypatch.setattr(email_service, "GMAIL_SCRIPT_SECRET", "s3cret")
    monkeypatch.setattr(email_service, "_script_configured", True)
    monkeypatch.setattr(email_service, "_resend_configured", False)
    return types.SimpleNamespace(calls=calls, state=state)


def test_sends_through_gmail_script(script):
    assert email_service._send("new.user@gmail.com", "Code", "<p>123456</p>", reply_to="r@x.com") is True
    call = script.calls[0]
    assert call["follow_redirects"] is True
    assert call["json"] == {
        "secret": "s3cret", "to": "new.user@gmail.com", "subject": "Code",
        "html": "<p>123456</p>", "name": "Synapse AI", "replyTo": "r@x.com",
    }


def test_script_refusal_is_not_sent(script):
    script.state["response"] = FakeResponse({"ok": False, "error": "unauthorized"})
    assert email_service._send("a@b.com", "s", "h") is False


def test_script_html_error_page_is_not_sent(script):
    script.state["response"] = FakeResponse(None)          # Google error page, not JSON
    assert email_service._send("a@b.com", "s", "h") is False


def test_script_network_error_is_not_sent(script):
    script.state["response"] = httpx.ConnectTimeout("slow")
    assert email_service._send("a@b.com", "s", "h") is False


def test_falls_back_to_resend_when_script_fails(script, monkeypatch):
    script.state["response"] = FakeResponse({"ok": False, "error": "quota"})
    sent = []
    fake_resend = types.SimpleNamespace(Emails=types.SimpleNamespace(send=lambda p: sent.append(p)))
    monkeypatch.setitem(__import__("sys").modules, "resend", fake_resend)
    monkeypatch.setattr(email_service, "_resend_configured", True)
    assert email_service._send("owner@gmail.com", "s", "h") is True
    assert sent and sent[0]["to"] == ["owner@gmail.com"]


def test_verification_email_goes_through_send(script):
    assert email_service.send_verification_email("p@example.com", "tok", "654321") is True
    assert script.calls[0]["json"]["subject"].startswith("654321 ")
