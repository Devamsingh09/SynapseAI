"""
Tests for persisted conversation titles: the pure helpers in thread_titles.py
and the auth.db storage (including migrating an existing database that
predates the title column). No network, no Groq.
"""
import importlib
import os
import sqlite3
import sys

import pytest

import thread_titles as tt


# ── thread_titles: pure helpers ──────────────────────────────────────────────

def test_short_message_is_its_own_title_without_llm():
    calls = []
    title, persist = tt.make_title("pandas help", lambda s: calls.append(s) or "X")
    assert (title, persist) == ("pandas help", True)
    assert calls == []


def test_long_message_uses_cleaned_llm_title():
    title, persist = tt.make_title(
        "How do I read a CSV file with pandas and plot it?",
        lambda s: '  Title: "Pandas CSV Plotting" ',
    )
    assert (title, persist) == ("Pandas CSV Plotting", True)


@pytest.mark.parametrize("llm", [lambda s: None, lambda s: "   ", lambda s: 1 / 0])
def test_llm_failure_falls_back_to_message_and_is_not_persisted(llm):
    src = "Explain the difference between RAG and fine-tuning in detail please"
    title, persist = tt.make_title(src, llm)
    assert persist is False
    assert title.startswith("Explain the difference")
    assert len(title) <= tt.FALLBACK_LEN


def test_empty_source_gives_default():
    assert tt.make_title("", lambda s: "x") == (tt.DEFAULT_TITLE, False)


def test_runaway_llm_title_is_capped():
    title, _ = tt.make_title("a long enough first message here", lambda s: "word " * 40)
    assert len(title) <= tt.MAX_GENERATED_LEN and title.endswith("…")


def test_title_source_ignores_extracted_file_text():
    with_text = 'Summarise this\n\n[Attached pdf: report.pdf]\n"""\nsecret contents\n"""'
    assert tt.title_source(with_text) == "Summarise this"
    file_only = '[Attached docx: notes.docx]\n"""\nsecret contents\n"""'
    assert tt.title_source(file_only) == "notes.docx"
    assert tt.title_source(None) == ""


def test_normalize_user_title():
    assert tt.normalize_user_title("  My   project  notes ") == "My project notes"
    with pytest.raises(ValueError):
        tt.normalize_user_title("   ")
    with pytest.raises(ValueError):
        tt.normalize_user_title("x" * (tt.MAX_USER_TITLE_LEN + 1))


# ── auth.db storage ──────────────────────────────────────────────────────────

def _fresh_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    sys.modules.pop("auth", None)
    return importlib.import_module("auth")


@pytest.fixture
def auth_db(tmp_path, monkeypatch):
    auth = _fresh_auth(tmp_path, monkeypatch)
    auth._conn.execute(
        "INSERT INTO users (email, password_hash, is_verified, created_at) VALUES (?, 'x', 1, 'now')",
        ("a@example.com",),
    )
    auth._conn.execute(
        "INSERT INTO users (email, password_hash, is_verified, created_at) VALUES (?, 'x', 1, 'now')",
        ("b@example.com",),
    )
    auth._conn.commit()
    yield auth
    auth._conn.close()
    sys.modules.pop("auth", None)


def test_title_roundtrip_and_listing(auth_db):
    auth_db.claim_thread("t1", 1)
    auth_db.claim_thread("t2", 1)
    auth_db.claim_thread("t3", 2)
    assert auth_db.get_thread_title("t1") is None

    assert auth_db.set_thread_title("t1", "Pandas CSV Plotting") is True
    auth_db.set_thread_title("t3", "Other user's chat")

    assert auth_db.get_thread_title("t1") == "Pandas CSV Plotting"
    assert auth_db.list_user_thread_titles(1) == {"t1": "Pandas CSV Plotting"}   # t2 untitled, t3 not theirs


def test_generated_title_never_overwrites_existing(auth_db):
    auth_db.claim_thread("t1", 1)
    auth_db.set_thread_title("t1", "Renamed by user")
    assert auth_db.set_thread_title("t1", "LLM title", only_if_empty=True) is False
    assert auth_db.get_thread_title("t1") == "Renamed by user"
    # an explicit rename does overwrite
    auth_db.set_thread_title("t1", "Renamed again")
    assert auth_db.get_thread_title("t1") == "Renamed again"


def test_title_removed_with_thread(auth_db):
    auth_db.claim_thread("t1", 1)
    auth_db.set_thread_title("t1", "Temp")
    auth_db.delete_thread_ownership("t1")
    assert auth_db.get_thread_title("t1") is None
    assert auth_db.set_thread_title("t1", "Ghost") is False   # no row to update


def test_migration_adds_title_column_to_existing_db(tmp_path, monkeypatch):
    # A database created by the previous version (no `title` column).
    conn = sqlite3.connect(os.path.join(tmp_path, "auth.db"))
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, is_verified INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
        CREATE TABLE thread_owners (thread_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL);
        INSERT INTO users (email, password_hash, is_verified, created_at) VALUES ('old@example.com', 'x', 1, 'then');
        INSERT INTO thread_owners VALUES ('old-thread', 1, 'then');
        """
    )
    conn.commit()
    conn.close()

    auth = _fresh_auth(tmp_path, monkeypatch)
    try:
        assert auth.get_thread_owner("old-thread") == 1          # existing data kept
        assert auth.get_thread_title("old-thread") is None
        assert auth.set_thread_title("old-thread", "Now titled") is True
        assert auth.list_user_thread_titles(1) == {"old-thread": "Now titled"}
    finally:
        auth._conn.close()
        sys.modules.pop("auth", None)
