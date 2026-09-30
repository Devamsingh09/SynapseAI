"""
Self-hosted authentication for Synapse AI — no external auth provider.

Storage: a dedicated SQLite file (backend/auth.db), separate from the
LangGraph checkpointer's chatbot.db, so this feature can't interfere with
(or be broken by) the existing chat graph/persistence.

Design:
- Passwords hashed with bcrypt (never stored/logged in plaintext).
- Sessions are a JWT stored in an httpOnly cookie (not readable by JS —
  safer against XSS than localStorage). Cookie name: "access_token".
- Email verification / password reset use single-use, expiring random
  tokens stored in `email_tokens` (not the JWT itself), so they can be
  invalidated after one use independently of the session token.
- `thread_owners` maps a LangGraph thread_id to the user who created it,
  so chat threads can be scoped per-user without touching chatbot_backend.py.
"""
from __future__ import annotations

import os
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from dotenv import load_dotenv
from fastapi import Cookie, HTTPException, status

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
# Load independently rather than relying on chatbot_backend.py having done
# it first — main.py imports this module before chatbot_backend, so without
# this, os.getenv() below would always miss values set only in .env.
load_dotenv(os.path.join(_BACKEND_DIR, ".env"), override=False)

# DATA_DIR lets deployments with an ephemeral container filesystem (e.g. a
# free Hugging Face Space, which wipes non-repo files on every rebuild)
# point auth.db at an attached persistent volume instead of the repo
# checkout. Defaults to the backend directory — unchanged for local dev.
# Kept in sync with chatbot_backend.py's own DATA_DIR handling for chatbot.db.
_DATA_DIR = os.getenv("DATA_DIR", _BACKEND_DIR)
os.makedirs(_DATA_DIR, exist_ok=True)
_DB_PATH = os.path.join(_DATA_DIR, "auth.db")

JWT_SECRET = os.getenv("JWT_SECRET") or ""
if not JWT_SECRET:
    # Fail-soft, not fail-silent: the app stays usable for local/dev testing,
    # but every restart invalidates all sessions. Loud on purpose.
    JWT_SECRET = secrets.token_hex(32)
    print(
        "WARNING: JWT_SECRET is not set in backend/.env — generated a random "
        "one for this process. Existing login sessions will be invalidated "
        "on every restart until you set a persistent JWT_SECRET.",
        flush=True,
    )

JWT_ALGORITHM = "HS256"
JWT_EXPIRE_DAYS = int(os.getenv("JWT_EXPIRE_DAYS", "7"))
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")

VERIFY_TOKEN_TTL_HOURS = 24
RESET_TOKEN_TTL_HOURS = 1

_lock = threading.Lock()
_conn = sqlite3.connect(_DB_PATH, check_same_thread=False, timeout=30)
_conn.execute("PRAGMA journal_mode=WAL")
_conn.execute("PRAGMA busy_timeout=30000")
_conn.execute("PRAGMA foreign_keys=ON")


def _init_schema() -> None:
    with _lock:
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_verified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS email_tokens (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                purpose TEXT NOT NULL CHECK (purpose IN ('verify_email', 'reset_password')),
                expires_at TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS thread_owners (
                thread_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL
            );
            """
        )
        _conn.commit()


_init_schema()


@dataclass
class User:
    id: int
    email: str
    is_verified: bool


# ─────────────────────────────────────────────────────────────────────────────
# Password hashing
# ─────────────────────────────────────────────────────────────────────────────


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# User storage
# ─────────────────────────────────────────────────────────────────────────────


def create_user(email: str, password: str) -> User:
    email = email.strip().lower()
    password_hash = hash_password(password)
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        cur = _conn.cursor()
        try:
            cur.execute(
                "INSERT INTO users (email, password_hash, is_verified, created_at) VALUES (?, ?, 0, ?)",
                (email, password_hash, now),
            )
            _conn.commit()
        except sqlite3.IntegrityError:
            raise ValueError("An account with that email already exists.")
        return User(id=cur.lastrowid, email=email, is_verified=False)


def get_user_by_email(email: str) -> Optional[dict]:
    email = email.strip().lower()
    with _lock:
        cur = _conn.cursor()
        cur.execute(
            "SELECT id, email, password_hash, is_verified FROM users WHERE email = ?",
            (email,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {"id": row[0], "email": row[1], "password_hash": row[2], "is_verified": bool(row[3])}


def get_user_by_id(user_id: int) -> Optional[User]:
    with _lock:
        cur = _conn.cursor()
        cur.execute("SELECT id, email, is_verified FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()
    if not row:
        return None
    return User(id=row[0], email=row[1], is_verified=bool(row[2]))


def mark_verified(user_id: int) -> None:
    with _lock:
        _conn.execute("UPDATE users SET is_verified = 1 WHERE id = ?", (user_id,))
        _conn.commit()


def update_password(user_id: int, new_password: str) -> None:
    with _lock:
        _conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), user_id),
        )
        _conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Email verification / password-reset tokens (single-use, expiring)
# ─────────────────────────────────────────────────────────────────────────────


def create_email_token(user_id: int, purpose: str, ttl_hours: int) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat()
    with _lock:
        _conn.execute(
            "INSERT INTO email_tokens (token, user_id, purpose, expires_at, used) VALUES (?, ?, ?, ?, 0)",
            (token, user_id, purpose, expires_at),
        )
        _conn.commit()
    return token


def consume_email_token(token: str, purpose: str) -> Optional[int]:
    """Validate + mark a token used in one step. Returns user_id, or None if
    the token is missing, wrong purpose, expired, or already used."""
    with _lock:
        cur = _conn.cursor()
        cur.execute(
            "SELECT user_id, purpose, expires_at, used FROM email_tokens WHERE token = ?",
            (token,),
        )
        row = cur.fetchone()
        if not row:
            return None
        user_id, tok_purpose, expires_at, used = row
        if tok_purpose != purpose or used:
            return None
        if datetime.fromisoformat(expires_at) < datetime.now(timezone.utc):
            return None
        _conn.execute("UPDATE email_tokens SET used = 1 WHERE token = ?", (token,))
        _conn.commit()
        return user_id


def create_verification_token(user_id: int) -> str:
    return create_email_token(user_id, "verify_email", VERIFY_TOKEN_TTL_HOURS)


def create_reset_token(user_id: int) -> str:
    return create_email_token(user_id, "reset_password", RESET_TOKEN_TTL_HOURS)


# ─────────────────────────────────────────────────────────────────────────────
# Thread ownership (per-user scoping of chat threads)
# ─────────────────────────────────────────────────────────────────────────────


def claim_thread(thread_id: str, user_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        _conn.execute(
            "INSERT OR IGNORE INTO thread_owners (thread_id, user_id, created_at) VALUES (?, ?, ?)",
            (thread_id, user_id, now),
        )
        _conn.commit()


def get_thread_owner(thread_id: str) -> Optional[int]:
    with _lock:
        cur = _conn.cursor()
        cur.execute("SELECT user_id FROM thread_owners WHERE thread_id = ?", (thread_id,))
        row = cur.fetchone()
    return row[0] if row else None


def list_user_thread_ids(user_id: int) -> set:
    with _lock:
        cur = _conn.cursor()
        cur.execute("SELECT thread_id FROM thread_owners WHERE user_id = ?", (user_id,))
        return {r[0] for r in cur.fetchall()}


def delete_thread_ownership(thread_id: str) -> None:
    with _lock:
        _conn.execute("DELETE FROM thread_owners WHERE thread_id = ?", (thread_id,))
        _conn.commit()


def require_thread_owner(thread_id: str, user_id: int) -> None:
    """Raise 404 if the thread isn't owned by this user (404, not 403, so we
    don't leak whether a thread_id exists at all to other users)."""
    owner = get_thread_owner(thread_id)
    if owner != user_id:
        raise HTTPException(status_code=404, detail="Thread not found")


# ─────────────────────────────────────────────────────────────────────────────
# JWT session tokens
# ─────────────────────────────────────────────────────────────────────────────


def create_access_token(user_id: int, email: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "email": email,
        "iat": now,
        "exp": now + timedelta(days=JWT_EXPIRE_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None


COOKIE_NAME = "access_token"
# secure=False so login works over plain http on localhost during dev; set
# FRONTEND_ORIGIN to an https:// origin in production and flip this via env.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").strip().lower() == "true"


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=JWT_EXPIRE_DAYS * 24 * 3600,
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI dependency
# ─────────────────────────────────────────────────────────────────────────────


def get_current_user(access_token: Optional[str] = Cookie(default=None)) -> User:
    if not access_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    payload = decode_access_token(access_token)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired or invalid")
    user = get_user_by_id(int(payload["sub"]))
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User no longer exists")
    return user
