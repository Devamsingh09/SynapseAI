"""
Feedback form: input rules, a per-user rate limit, and the email body.

The sender's email always comes from their login session, never from the
form, so a feedback mail can't claim to be from someone else. Everything a
user typed is HTML-escaped before it goes into the email.
"""
import html
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

MAX_NAME_LEN = 80
MIN_MESSAGE_LEN = 3
MAX_MESSAGE_LEN = 1000

# 5 feedback mails per user per hour is plenty for real use and keeps the
# Resend free tier (100/day) safe from a stuck or abusive client.
RATE_LIMIT = 5
RATE_WINDOW_SEC = 3600


def normalize_name(value) -> str:
    # Collapsing whitespace also removes newlines, so the name is safe to put
    # in the subject line.
    name = " ".join(str(value or "").split())
    if not name:
        raise ValueError("Please enter your name.")
    if len(name) > MAX_NAME_LEN:
        raise ValueError(f"Name must be {MAX_NAME_LEN} characters or fewer.")
    return name


def normalize_message(value) -> str:
    message = str(value or "").replace("\r\n", "\n").strip()
    if len(message) < MIN_MESSAGE_LEN:
        raise ValueError("Please write a little feedback before sending.")
    if len(message) > MAX_MESSAGE_LEN:
        raise ValueError(f"Feedback must be {MAX_MESSAGE_LEN} characters or fewer.")
    return message


class RateLimiter:
    """Sliding-window limit per key (in memory; resets when the Space restarts)."""

    def __init__(self, limit: int = RATE_LIMIT, window_sec: float = RATE_WINDOW_SEC, clock=time.monotonic):
        self.limit = limit
        self.window = window_sec
        self._clock = clock
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key) -> bool:
        now = self._clock()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True

    def refund(self, key) -> None:
        """Give back the last hit, e.g. when the email failed to send."""
        with self._lock:
            hits = self._hits.get(key)
            if hits:
                hits.pop()


def build_email(name: str, user_email: str, message: str, user_id=None, sent_at=None):
    """Return (subject, html) for the feedback email."""
    sent_at = sent_at or datetime.now(timezone.utc)
    esc_name = html.escape(name)
    esc_email = html.escape(user_email)
    esc_message = html.escape(message).replace("\n", "<br>")
    when = sent_at.strftime("%d %b %Y, %H:%M UTC")
    account = f" · account #{html.escape(str(user_id))}" if user_id is not None else ""

    subject = f"SynapseAI feedback from {name}"
    body = (
        '<div style="font-family:Segoe UI,Arial,sans-serif;font-size:15px;color:#1f2937;max-width:560px">'
        '<h2 style="margin:0 0 12px;font-size:18px;color:#4f46e5">New feedback on SynapseAI</h2>'
        '<table style="border-collapse:collapse;margin-bottom:16px">'
        f'<tr><td style="padding:4px 12px 4px 0;color:#6b7280">Name</td><td style="padding:4px 0"><strong>{esc_name}</strong></td></tr>'
        f'<tr><td style="padding:4px 12px 4px 0;color:#6b7280">Email</td><td style="padding:4px 0"><a href="mailto:{esc_email}">{esc_email}</a></td></tr>'
        f'<tr><td style="padding:4px 12px 4px 0;color:#6b7280">Sent</td><td style="padding:4px 0">{when}{account}</td></tr>'
        "</table>"
        '<div style="padding:14px 16px;background:#f3f4f6;border-left:3px solid #6366f1;border-radius:6px;line-height:1.55">'
        f"{esc_message}</div>"
        f'<p style="margin-top:16px;font-size:13px;color:#6b7280">Reply to this email to answer {esc_name} directly.</p>'
        "</div>"
    )
    return subject, body
