"""
Email verification codes: storage rules (auth.py), the email (email_service)
and the signup → code → signed-in flow (main.py routes).
"""
import importlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

import email_service


def _fresh_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    sys.modules.pop("auth", None)
    return importlib.import_module("auth")


@pytest.fixture
def auth_db(tmp_path, monkeypatch):
    original = sys.modules.get("auth")
    auth = _fresh_auth(tmp_path, monkeypatch)
    yield auth
    auth._conn.close()
    # Put back the module main.py was imported with, so later tests'
    # dependency overrides (keyed on auth.get_current_user) still match.
    if original is not None:
        sys.modules["auth"] = original
    else:
        sys.modules.pop("auth", None)


def _user(auth, email="new@example.com"):
    return auth.create_user(email, "password123").id


# ── storage ──────────────────────────────────────────────────────────────────

def test_code_is_six_digits_and_single_use(auth_db):
    uid = _user(auth_db)
    code = auth_db.create_verification_otp(uid)
    assert len(code) == 6 and code.isdigit()
    assert auth_db.check_verification_otp(uid, code) == "ok"
    assert auth_db.check_verification_otp(uid, code) == "missing"


def test_code_not_stored_in_plain_text(auth_db):
    uid = _user(auth_db)
    code = auth_db.create_verification_otp(uid)
    stored = auth_db._conn.execute("SELECT code_hash FROM email_otps").fetchone()[0]
    assert code not in stored


def test_spaces_and_dashes_are_accepted(auth_db):
    uid = _user(auth_db)
    code = auth_db.create_verification_otp(uid)
    assert auth_db.check_verification_otp(uid, f" {code[:3]}-{code[3:]} ") == "ok"


def test_new_code_replaces_old_one(auth_db):
    uid = _user(auth_db)
    first = auth_db.create_verification_otp(uid)
    second = auth_db.create_verification_otp(uid)
    if first != second:
        assert auth_db.check_verification_otp(uid, first) == "wrong"
    assert auth_db.check_verification_otp(uid, second) == "ok"


def test_expired_code(auth_db):
    uid = _user(auth_db)
    past = datetime.now(timezone.utc) - timedelta(minutes=auth_db.OTP_TTL_MINUTES + 1)
    code = auth_db.create_verification_otp(uid, now=past)
    assert auth_db.check_verification_otp(uid, code) == "expired"


def test_locks_after_too_many_wrong_tries(auth_db):
    uid = _user(auth_db)
    code = auth_db.create_verification_otp(uid)
    wrong = "000000" if code != "000000" else "111111"
    results = [auth_db.check_verification_otp(uid, wrong) for _ in range(auth_db.OTP_MAX_ATTEMPTS)]
    assert results[:-1] == ["wrong"] * (auth_db.OTP_MAX_ATTEMPTS - 1) and results[-1] == "locked"
    # even the right code no longer works until a new one is sent
    assert auth_db.check_verification_otp(uid, code) == "locked"
    fresh = auth_db.create_verification_otp(uid)
    assert auth_db.check_verification_otp(uid, fresh) == "ok"


def test_resend_cooldown(auth_db):
    uid = _user(auth_db)
    assert not auth_db.otp_sent_recently(uid)
    now = datetime.now(timezone.utc)
    auth_db.create_verification_otp(uid, now=now)
    assert auth_db.otp_sent_recently(uid, now=now + timedelta(seconds=5))
    assert not auth_db.otp_sent_recently(uid, now=now + timedelta(seconds=auth_db.OTP_RESEND_COOLDOWN_SEC + 1))


def test_link_verification_clears_code(auth_db):
    uid = _user(auth_db)
    code = auth_db.create_verification_otp(uid)
    auth_db.mark_verified(uid)
    assert auth_db.check_verification_otp(uid, code) == "missing"


# ── email ────────────────────────────────────────────────────────────────────

def test_email_has_code_in_subject_and_body_plus_link():
    subject, html = email_service.build_verification_email("tok123", "482913")
    assert subject.startswith("482913 ")
    assert "482913" in html and "/verify-email?token=tok123" in html


# ── routes ───────────────────────────────────────────────────────────────────

@pytest.fixture
def main_module():
    # Imported before auth_db swaps in a temporary auth module, so main.py
    # stays bound to the real one for every other test file.
    return pytest.importorskip("main")


@pytest.fixture
def client(main_module, auth_db, monkeypatch):
    main = main_module
    from fastapi.testclient import TestClient

    # Routes captured the real auth.get_current_user at import; point it at the temp DB.
    main.app.dependency_overrides[main.auth.get_current_user] = auth_db.get_current_user
    monkeypatch.setattr(main, "auth", auth_db)
    sent = []
    monkeypatch.setattr(
        email_service, "send_verification_email",
        lambda email, token, code=None: sent.append({"email": email, "token": token, "code": code}) or True,
    )
    c = TestClient(main.app)
    c.sent = sent
    yield c
    main.app.dependency_overrides.clear()


def test_signup_then_code_signs_in(client):
    r = client.post("/auth/signup", json={"email": "Phone@Example.com", "password": "password123"})
    assert r.status_code == 201
    code = client.sent[-1]["code"]
    assert code and client.sent[-1]["token"]

    r = client.post("/auth/verify-otp", json={"email": "phone@example.com", "code": code})
    assert r.status_code == 200 and r.json()["is_verified"] is True
    assert "access_token" in r.cookies or "access_token" in client.cookies
    assert client.get("/auth/me").json()["email"] == "phone@example.com"

    # password login works afterwards too
    r = client.post("/auth/login", json={"email": "phone@example.com", "password": "password123"})
    assert r.status_code == 200


def test_wrong_code_is_400_and_not_signed_in(client):
    client.post("/auth/signup", json={"email": "a@example.com", "password": "password123"})
    code = client.sent[-1]["code"]
    wrong = "000000" if code != "000000" else "111111"
    r = client.post("/auth/verify-otp", json={"email": "a@example.com", "code": wrong})
    assert r.status_code == 400 and "isn't right" in r.json()["detail"]
    assert client.get("/auth/me").status_code == 401


def test_unknown_email_gives_same_error(client):
    r = client.post("/auth/verify-otp", json={"email": "nobody@example.com", "code": "123456"})
    assert r.status_code == 400 and "isn't right" in r.json()["detail"]


def test_already_verified(client):
    client.post("/auth/signup", json={"email": "v@example.com", "password": "password123"})
    code = client.sent[-1]["code"]
    client.post("/auth/verify-otp", json={"email": "v@example.com", "code": code})
    r = client.post("/auth/verify-otp", json={"email": "v@example.com", "code": code})
    assert r.status_code == 409


def test_resend_sends_new_code_but_respects_cooldown(client, auth_db, monkeypatch):
    client.post("/auth/signup", json={"email": "r@example.com", "password": "password123"})
    assert len(client.sent) == 1
    client.post("/auth/resend-verification", json={"email": "r@example.com"})
    assert len(client.sent) == 1                     # within cooldown: nothing new
    monkeypatch.setattr(auth_db, "otp_sent_recently", lambda uid, now=None: False)
    client.post("/auth/resend-verification", json={"email": "r@example.com"})
    assert len(client.sent) == 2
    r = client.post("/auth/verify-otp", json={"email": "r@example.com", "code": client.sent[-1]["code"]})
    assert r.status_code == 200
