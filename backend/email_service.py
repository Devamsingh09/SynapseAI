"""
Transactional email for auth flows (verification + password reset) and
feedback.

Senders, tried in order:
1. Gmail via a Google Apps Script web app (GMAIL_SCRIPT_URL +
   GMAIL_SCRIPT_SECRET; script in backend/gmail_sender.gs). Mail comes from
   the owner's Gmail and can go to anyone (~100 recipients/day). Used
   because Hugging Face Spaces can't reach SMTP servers, and Resend's shared
   test sender only delivers to the Resend account's own address.
2. Resend (RESEND_API_KEY) — also the fallback if the script fails.
3. Neither set: emails are printed to the server log (local dev).

Never raises on send failure; callers treat "email didn't send" as
non-fatal so a flaky mail provider never blocks signup.
"""
import os

import httpx
from dotenv import load_dotenv

import feedback

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_BACKEND_DIR, ".env"), override=False)

RESEND_API_KEY = (os.getenv("RESEND_API_KEY") or "").strip()
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "Synapse AI <onboarding@resend.dev>")
# Where the in-app feedback form is delivered. With Resend's shared
# onboarding@resend.dev sender this must be the Resend account's own email.
FEEDBACK_TO_EMAIL = (os.getenv("FEEDBACK_TO_EMAIL") or "devamsingh0009@gmail.com").strip()
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")

GMAIL_SCRIPT_URL = (os.getenv("GMAIL_SCRIPT_URL") or "").strip()
GMAIL_SCRIPT_SECRET = (os.getenv("GMAIL_SCRIPT_SECRET") or "").strip()
# The owner's own name: the Gmail address is personal (also used for job
# applications), so mail from it shouldn't appear under the product name.
SENDER_NAME = os.getenv("EMAIL_SENDER_NAME", "Devam Singh")
SCRIPT_TIMEOUT_SEC = 25

_script_configured = bool(GMAIL_SCRIPT_URL and GMAIL_SCRIPT_SECRET)
_resend_configured = bool(RESEND_API_KEY)
if _resend_configured:
    import resend

    resend.api_key = RESEND_API_KEY


def _send_via_gmail_script(to_email: str, subject: str, html: str, reply_to: str = None) -> bool:
    payload = {
        "secret": GMAIL_SCRIPT_SECRET,
        "to": to_email,
        "subject": subject,
        "html": html,
        "name": SENDER_NAME,
    }
    if reply_to:
        payload["replyTo"] = reply_to
    try:
        # Apps Script answers a POST with a redirect to the result; httpx
        # follows it with a GET, which is how these web apps are meant to be called.
        r = httpx.post(GMAIL_SCRIPT_URL, json=payload, timeout=SCRIPT_TIMEOUT_SEC, follow_redirects=True)
        data = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        print(f"[email_service] Gmail script failed for {to_email}: {type(exc).__name__}: {exc}", flush=True)
        return False
    if isinstance(data, dict) and data.get("ok"):
        return True
    error = data.get("error") if isinstance(data, dict) else data
    print(f"[email_service] Gmail script refused {to_email}: {error}", flush=True)
    return False


def _send(to_email: str, subject: str, html: str, reply_to: str = None) -> bool:
    if _script_configured and _send_via_gmail_script(to_email, subject, html, reply_to):
        return True
    if not _resend_configured:
        if _script_configured:
            return False
        # flush=True — see the note on adaptive_loop's logging: this may run
        # off the main thread via FastAPI's sync-route threadpool.
        print(
            f"[email_service] RESEND_API_KEY not set — would send to {to_email}: "
            f"{subject}\n{html}",
            flush=True,
        )
        return False
    try:
        import resend

        params = {"from": RESEND_FROM_EMAIL, "to": [to_email], "subject": subject, "html": html}
        if reply_to:
            params["reply_to"] = reply_to
        resend.Emails.send(params)
        return True
    except Exception as exc:
        print(f"[email_service] send failed to {to_email}: {type(exc).__name__}: {exc}", flush=True)
        return False


def build_verification_email(token: str, code: str = None) -> tuple:
    """(subject, html). With a code, it leads (and is in the subject, so it
    shows in the phone's notification); the link stays as the other option."""
    link = f"{FRONTEND_ORIGIN}/verify-email?token={token}"
    if not code:
        html = (
            f"<p>Welcome to Synapse AI — confirm your email to activate your account.</p>"
            f'<p><a href="{link}">Verify your email</a></p>'
            f"<p>This link expires in 24 hours. If you didn't sign up, you can ignore this email.</p>"
        )
        return "Verify your Synapse AI account", html
    html = (
        '<div style="font-family:Segoe UI,Helvetica,Arial,sans-serif;max-width:480px;color:#1f2937">'
        "<p>Welcome to Synapse AI! Enter this code to verify your email and sign in:</p>"
        '<p style="font-size:32px;font-weight:700;letter-spacing:8px;margin:20px 0;'
        'font-family:Consolas,Menlo,monospace;color:#4f46e5">'
        f"{code}</p>"
        "<p style=\"color:#6b7280\">The code works for 15 minutes.</p>"
        f'<p>Or <a href="{link}">verify with this link</a> (works for 24 hours).</p>'
        "<p style=\"color:#6b7280\">If you didn't sign up, you can ignore this email.</p>"
        "</div>"
    )
    return f"{code} is your Synapse AI verification code", html


def send_verification_email(to_email: str, token: str, code: str = None) -> bool:
    subject, html = build_verification_email(token, code)
    return _send(to_email, subject, html)


def send_password_reset_email(to_email: str, token: str) -> bool:
    link = f"{FRONTEND_ORIGIN}/reset-password?token={token}"
    html = (
        f"<p>Someone requested a password reset for this Synapse AI account.</p>"
        f'<p><a href="{link}">Reset your password</a></p>'
        f"<p>This link expires in 1 hour. If you didn't request this, you can ignore this email.</p>"
    )
    return _send(to_email, "Reset your Synapse AI password", html)


def send_feedback_email(name: str, user_email: str, message: str, user_id=None) -> bool:
    """Deliver in-app feedback to the creator; replies go straight to the user."""
    subject, html = feedback.build_email(name, user_email, message, user_id=user_id)
    return _send(FEEDBACK_TO_EMAIL, subject, html, reply_to=user_email)
