"""
Transactional email for auth flows (verification + password reset) via
Resend's free tier (https://resend.com — 100 emails/day / 3,000/month free).

Dev fallback: if RESEND_API_KEY isn't set, emails are printed to the server
log instead of sent — so signup/verify/reset can be exercised end-to-end
locally before you've created a Resend account. Never raises on send
failure; callers treat "email didn't send" as non-fatal (see auth routes
in main.py) so a flaky/unconfigured mail provider never blocks signup.
"""
import os

from dotenv import load_dotenv

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_BACKEND_DIR, ".env"), override=False)

RESEND_API_KEY = (os.getenv("RESEND_API_KEY") or "").strip()
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "Synapse AI <onboarding@resend.dev>")
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")

_resend_configured = bool(RESEND_API_KEY)
if _resend_configured:
    import resend

    resend.api_key = RESEND_API_KEY


def _send(to_email: str, subject: str, html: str) -> bool:
    if not _resend_configured:
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

        resend.Emails.send({"from": RESEND_FROM_EMAIL, "to": [to_email], "subject": subject, "html": html})
        return True
    except Exception as exc:
        print(f"[email_service] send failed to {to_email}: {type(exc).__name__}: {exc}", flush=True)
        return False


def send_verification_email(to_email: str, token: str) -> bool:
    link = f"{FRONTEND_ORIGIN}/verify-email?token={token}"
    html = (
        f"<p>Welcome to Synapse AI — confirm your email to activate your account.</p>"
        f'<p><a href="{link}">Verify your email</a></p>'
        f"<p>This link expires in 24 hours. If you didn't sign up, you can ignore this email.</p>"
    )
    return _send(to_email, "Verify your Synapse AI account", html)


def send_password_reset_email(to_email: str, token: str) -> bool:
    link = f"{FRONTEND_ORIGIN}/reset-password?token={token}"
    html = (
        f"<p>Someone requested a password reset for this Synapse AI account.</p>"
        f'<p><a href="{link}">Reset your password</a></p>'
        f"<p>This link expires in 1 hour. If you didn't request this, you can ignore this email.</p>"
    )
    return _send(to_email, "Reset your Synapse AI password", html)
