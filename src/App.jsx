import React, { useState, useEffect, useRef, useCallback, useMemo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import { v4 as uuid } from "uuid";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import {
  createVoiceSession,
  extractNewSpeechChunks,
  isVoiceSupported,
  setActiveVoice,
  readAloud,
  stopReadAloud,
} from "./voiceChat";
import { API_BASE } from "./apiConfig";
import { AuthProvider, useAuth } from "./AuthContext";
import { LoginPage, SignupPage, VerifyEmailPage, ForgotPasswordPage, ResetPasswordPage } from "./AuthPages";
import { BrandMark, Wordmark } from "./Brand";
import ToolDirectory, { TOOLS } from "./ToolDirectory";
import {
  PlusIcon, MenuIcon, CloseIcon, ChatIcon, ToolsIcon, TrashIcon, PencilIcon, PaperclipIcon,
  MicIcon, SendIcon, StopIcon, LogoutIcon, ChevronUpIcon, ChevronDownIcon, FileIcon, ImageIcon,
  AlertIcon, CopyIcon, CheckIcon, SearchIcon, CodeIcon, BookIcon, SparkIcon, SidebarIcon,
  VolumeIcon, RefreshIcon, ArrowDownIcon, ArrowRightIcon,
} from "./Icons";

// ═══════════════════════════════════════════════════════
// API
// ═══════════════════════════════════════════════════════
const BASE = API_BASE;

// Agent steps come from the status events the backend streams
// (`data: {"status": ...}`): `using_tools`, plus `evaluating` / `refining` /
// `finalizing` from the adaptive agent loop (backend/adaptive_loop.py).
// `using_tools` doesn't say WHICH tool ran, so the label stays generic.
const STEP_LABELS = {
  using_tools: { live: "Using tools…",          done: "Used tools" },
  evaluating:  { live: "Checking the results…", done: "Checked the results" },
  refining:    { live: "Refining the answer…",  done: "Refined the answer" },
  finalizing:  { live: "Finalizing…",           done: "Finalized" },
};

const ERROR_PREFIX = "⚠️ **Error:** ";

const api = {
  // credentials: "include" — every call to the protected backend needs to
  // send the httpOnly session cookie set by /auth/login.
  newThread:    ()    => fetch(`${BASE}/thread/new`, { method: "POST", credentials: "include" }).then(r => r.json()).then(d => d.thread_id),
  // { threads: [ids], titles: {id: storedTitle} } — `titles` is absent on
  // older backends, so default it.
  getThreads:   ()    => fetch(`${BASE}/threads`, { credentials: "include" }).then(r => r.json())
    .then(d => ({ threads: d.threads || [], titles: d.titles || {} })),

  getHistory:   (tid) => fetch(`${BASE}/thread/${tid}/history`, { credentials: "include" })
    .then(r => r.ok ? r.json() : { messages: [], title: "New Conversation" })
    .then(data => ({
      ...data,
      messages: data.messages || []
    })),

  deleteThread: (tid) => fetch(`${BASE}/thread/${tid}`, { method: "DELETE", credentials: "include" }),
  stopChat:     (tid) => fetch(`${BASE}/chat/stop`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ thread_id: tid }),
  }),
  // Passing thread_id makes the backend store the title (generated once).
  getSummary:   (txt, tid) => fetch(`${BASE}/chat/summary`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: txt, thread_id: tid })
  }).then(r => r.json()).then(d => d.title || "New Conversation"),

  renameThread: (tid, title) => fetch(`${BASE}/thread/${tid}/title`, {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  }).then(async r => {
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      // FastAPI validation errors arrive as a list of { msg }.
      const msg = Array.isArray(d.detail) ? d.detail[0]?.msg : d.detail;
      throw new Error((msg || `Rename failed (${r.status})`).replace(/^Value error, /, ""));
    }
    return d.title;
  }),

  uploadFile: (file) => {
    const fd = new FormData();
    fd.append("file", file);
    return fetch(`${BASE}/upload`, { method: "POST", credentials: "include", body: fd }).then(async r => {
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        throw new Error(d.detail || `Upload failed (${r.status})`);
      }
      return r.json(); // { kind, filename, content }
    });
  },

  async streamChat(threadId, message, onToken, onStatus, options = {}) {
    const { signal, voice = false } = options;
    const res = await fetch(`${BASE}/chat/stream`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ thread_id: threadId, message, voice }),
      signal,
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let full = "", buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n"); buffer = lines.pop();
      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        const payload = line.slice(6).trim();
        if (payload === "[DONE]") return full;
        try {
          const obj = JSON.parse(payload);
          if (obj.error) throw new Error(obj.error);
          if (obj.status) { onStatus?.(obj.status); continue; }
          if (obj.token) { full += obj.token; onToken(obj.token); }
        } catch (e) {
          if (e instanceof SyntaxError) continue;
          throw e;
        }
      }
    }
    return full;
  }
};

// /health is a real backend endpoint: { ok } is true when the model provider
// is configured. The status pill only ever reflects what it returns.
const HEALTH_LABELS = {
  checking: "Connecting…",
  ok: "Connected",
  degraded: "Service issue",
  down: "Unavailable",
};

function useMediaQuery(query) {
  const get = () => typeof window !== "undefined" && window.matchMedia?.(query).matches;
  const [matches, setMatches] = useState(get);
  useEffect(() => {
    const mq = window.matchMedia(query);
    const on = () => setMatches(mq.matches);
    mq.addEventListener?.("change", on);
    return () => mq.removeEventListener?.("change", on);
  }, [query]);
  return !!matches;
}

function readPref(key, fallback) {
  try { const v = localStorage.getItem(key); return v === null ? fallback : v; } catch { return fallback; }
}
function writePref(key, value) {
  try { localStorage.setItem(key, value); } catch { /* storage blocked: fine */ }
}

// ═══════════════════════════════════════════════════════
// TOP BAR
// ═══════════════════════════════════════════════════════
function TopBar({ view, health, isMobile, collapsed, onToggleNav, onNew, navBtnRef }) {
  return (
    <header className="topbar">
      <button
        ref={navBtnRef}
        type="button"
        className="icon-btn nav-toggle"
        onClick={onToggleNav}
        aria-label={isMobile ? "Open navigation" : collapsed ? "Expand sidebar" : "Collapse sidebar"}
        aria-expanded={isMobile ? undefined : !collapsed}
        title={isMobile ? "Menu" : collapsed ? "Expand sidebar" : "Collapse sidebar"}
      >
        {isMobile ? <MenuIcon size={19} /> : <SidebarIcon size={18} />}
      </button>
      <div className="topbar-brand">
        <BrandMark size={36} />
        <Wordmark />
      </div>
      <span className="crumb-sep" aria-hidden="true">/</span>
      <span className="crumb">{view === "tools" ? "AI Tools" : "AI Assistant"}</span>

      <div className="topbar-right">
        <span className="model-pill" title="Language model that writes the answers">GPT-OSS 120B</span>
        <span className={`health health-${health}`} role="status" title="Backend status from /health">
          <span className="health-dot" aria-hidden="true" />
          <span className="health-label">{HEALTH_LABELS[health]}</span>
        </span>
        <button type="button" className="icon-btn topbar-new" onClick={onNew} aria-label="New conversation">
          <PlusIcon size={19} />
        </button>
      </div>
    </header>
  );
}

// ═══════════════════════════════════════════════════════
// ACCOUNT MENU (sidebar footer)
// ═══════════════════════════════════════════════════════
// Log out sits one click behind the account row so a stray click can't end
// the session. Closes on outside click or Escape; focus returns to the row.
function AccountMenu({ email, onLogout }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const rootRef = useRef(null);
  const triggerRef = useRef(null);

  useEffect(() => {
    if (!open) return;
    const onPointer = e => { if (!rootRef.current?.contains(e.target)) setOpen(false); };
    const onKey = e => {
      if (e.key === "Escape") { e.stopPropagation(); setOpen(false); triggerRef.current?.focus(); }
    };
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("touchstart", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("touchstart", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const handleLogout = async () => {
    setBusy(true);
    // logout() always clears the session locally (even if the request fails),
    // and the auth gate then redirects to /login — this component unmounts.
    try { await onLogout(); } catch { setBusy(false); }
  };

  return (
    <div className="account" ref={rootRef}>
      {open && (
        <div className="account-menu" role="menu" aria-label="Account">
          <div className="account-menu-label">Signed in as</div>
          <div className="account-menu-email" title={email}>{email}</div>
          <div className="account-menu-sep" />
          <button className="account-menu-item danger" role="menuitem" onClick={handleLogout} disabled={busy} autoFocus>
            <LogoutIcon size={15} />
            {busy ? "Signing out…" : "Sign out"}
          </button>
        </div>
      )}
      <button
        ref={triggerRef}
        className={`account-trigger ${open ? "open" : ""}`}
        onClick={() => setOpen(o => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        title={email}
      >
        <span className="account-avatar" aria-hidden="true">{(email || "?")[0].toUpperCase()}</span>
        <span className="account-email">{email}</span>
        <ChevronUpIcon size={14} />
      </button>
    </div>
  );
}

// ═══════════════════════════════════════════════════════
// SIDEBAR
// ═══════════════════════════════════════════════════════
// One row in "Recent conversations": open, rename inline, delete.
// Rename: pencil button (or double-click the name) → text field.
// Enter or clicking away saves; Escape cancels. Max 80 characters (the
// backend enforces the same limit).
function ConversationItem({ tid, title, active, onLoad, onDelete, onRename }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(title);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const inputRef = useRef(null);
  const renameBtnRef = useRef(null);
  const skipBlurRef = useRef(false);

  const startEdit = () => {
    skipBlurRef.current = false;
    setDraft(title);
    setError(null);
    setEditing(true);
    requestAnimationFrame(() => inputRef.current?.select());
  };

  const finish = (focusBack = true) => {
    setEditing(false);
    if (focusBack) requestAnimationFrame(() => renameBtnRef.current?.focus());
  };

  const commit = async () => {
    const next = draft.replace(/\s+/g, " ").trim();
    if (!next || next === title) { finish(); return; }
    setSaving(true);
    try {
      await onRename(tid, next);
      setError(null);
      finish();
    } catch (err) {
      setError(err.message || "Couldn't rename this conversation.");
      requestAnimationFrame(() => inputRef.current?.focus());
    } finally {
      setSaving(false);
    }
  };

  if (editing) {
    return (
      <div className="t-item">
        <div className="t-row editing">
          <input
            ref={inputRef}
            className="t-edit"
            value={draft}
            maxLength={80}
            disabled={saving}
            aria-label="Conversation name"
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? `rename-err-${tid}` : undefined}
            onChange={e => setDraft(e.target.value)}
            onKeyDown={e => {
              if (e.key === "Enter") { e.preventDefault(); skipBlurRef.current = true; commit(); }
              if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); skipBlurRef.current = true; setError(null); finish(); }
            }}
            onBlur={() => {
              if (skipBlurRef.current) { skipBlurRef.current = false; return; }
              if (!saving) commit();
            }}
          />
          {saving && <span className="spinner small t-saving" aria-hidden="true" />}
        </div>
        {error && <div className="t-error" id={`rename-err-${tid}`} role="alert">{error}</div>}
      </div>
    );
  }

  return (
    <div className="t-item">
      <div className={`t-row ${active ? "active" : ""}`}>
        <button
          className="t-btn"
          onClick={() => onLoad(tid)}
          onDoubleClick={startEdit}
          title={title}
          aria-current={active ? "true" : undefined}
        >
          {title}
        </button>
        <div className="t-actions">
          <button
            ref={renameBtnRef}
            className="t-act"
            onClick={e => { e.stopPropagation(); startEdit(); }}
            aria-label={`Rename conversation: ${title}`}
            title="Rename"
          >
            <PencilIcon size={14} />
          </button>
          <button
            className="t-act danger"
            onClick={e => { e.stopPropagation(); onDelete(tid); }}
            aria-label={`Delete conversation: ${title}`}
            title="Delete"
          >
            <TrashIcon size={14} />
          </button>
        </div>
      </div>
    </div>
  );
}

function Sidebar({
  threads, titles, threadId, onNew, onLoad, onDelete, onRename, isOpen, onClose,
  view, onView, historyStatus, onRetryHistory, rail, onExpand,
}) {
  const { user, logout } = useAuth();
  const newBtnRef = useRef(null);
  const [query, setQuery] = useState("");

  // Mobile drawer: move focus in on open, close on Escape (spec §10).
  useEffect(() => {
    if (!isOpen) return;
    newBtnRef.current?.focus();
    const onKey = e => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [isOpen, onClose]);

  // Collapsed desktop sidebar: an icon rail.
  if (rail) {
    return (
      <nav className="rail" aria-label="Workspace navigation (collapsed)">
        <button type="button" className="rail-btn primary" onClick={onNew} aria-label="New conversation" title="New conversation (Ctrl+K)">
          <PlusIcon size={18} />
        </button>
        <button
          type="button"
          className={`rail-btn ${view === "assistant" ? "active" : ""}`}
          onClick={() => onView("assistant")}
          aria-label="AI Assistant"
          aria-current={view === "assistant" ? "page" : undefined}
          title="AI Assistant"
        >
          <ChatIcon size={18} />
        </button>
        <button
          type="button"
          className={`rail-btn ${view === "tools" ? "active" : ""}`}
          onClick={() => onView("tools")}
          aria-label="AI Tools"
          aria-current={view === "tools" ? "page" : undefined}
          title="AI Tools"
        >
          <ToolsIcon size={18} />
        </button>
        {user && (
          <button type="button" className="rail-avatar" onClick={onExpand} aria-label={`Account: ${user.email}. Expand sidebar`} title={user.email}>
            {(user.email || "?")[0].toUpperCase()}
          </button>
        )}
      </nav>
    );
  }

  const q = query.trim().toLowerCase();
  const recent = [...threads].reverse().filter(tid => !q || (titles[tid] || "New conversation").toLowerCase().includes(q));

  return (
    <>
      <div className={`overlay ${isOpen ? "open" : ""}`} onClick={onClose} aria-hidden="true" />
      <aside className={`sidebar ${isOpen ? "open" : ""}`} aria-label="Workspace navigation">
        <div className="drawer-head">
          <div className="topbar-brand">
            <BrandMark size={32} />
            <Wordmark />
          </div>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close navigation">
            <CloseIcon size={18} />
          </button>
        </div>

        <div className="side-top">
          <button ref={newBtnRef} className="btn-primary btn-new" onClick={onNew}>
            <PlusIcon size={16} />
            New conversation
            <kbd className="kbd">Ctrl K</kbd>
          </button>
          <label className="side-search">
            <SearchIcon size={15} />
            <span className="sr-only">Search conversations</span>
            <input
              type="search"
              value={query}
              onChange={e => setQuery(e.target.value)}
              placeholder="Search conversations"
            />
          </label>
        </div>

        <nav className="side-nav" aria-label="Workspace">
          <span className="side-label">Workspace</span>
          <button
            type="button"
            className={`nav-item ${view === "assistant" ? "active" : ""}`}
            aria-current={view === "assistant" ? "page" : undefined}
            onClick={() => onView("assistant")}
          >
            <ChatIcon size={16} /> AI Assistant
          </button>
          <button
            type="button"
            className={`nav-item ${view === "tools" ? "active" : ""}`}
            aria-current={view === "tools" ? "page" : undefined}
            onClick={() => onView("tools")}
          >
            <ToolsIcon size={16} /> AI Tools
            <span className="nav-badge" aria-label={`${TOOLS.length} tools`}>{TOOLS.length}</span>
          </button>
        </nav>

        <nav className="history" aria-label="Recent conversations">
          <span className="side-label">Recent conversations</span>
          {recent.map(tid => (
            <ConversationItem
              key={tid}
              tid={tid}
              title={titles[tid] || "New conversation"}
              active={tid === threadId && view === "assistant"}
              onLoad={onLoad}
              onDelete={onDelete}
              onRename={onRename}
            />
          ))}
          {q && recent.length === 0 && threads.length > 0 && (
            <div className="history-state">No conversations match “{query.trim()}”.</div>
          )}
          {threads.length === 0 && historyStatus === "loading" && (
            <div className="history-state" role="status">
              <span className="spinner small" aria-hidden="true" /> Loading conversations…
            </div>
          )}
          {threads.length === 0 && historyStatus === "ready" && (
            <div className="history-state">No conversations yet</div>
          )}
          {historyStatus === "error" && (
            <div className="history-state error" role="alert">
              Couldn't load conversations.
              <button type="button" className="link-btn" onClick={onRetryHistory}>Try again</button>
            </div>
          )}
        </nav>

        {user && <AccountMenu email={user.email} onLogout={logout} />}
      </aside>
    </>
  );
}

// ═══════════════════════════════════════════════════════
// CONVERSATION HEADER (title · rename · delete)
// ═══════════════════════════════════════════════════════
function ConversationHeader({ title, count, onRename, onDelete }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(title);
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);
  const inputRef = useRef(null);
  const skipBlurRef = useRef(false);

  const start = () => {
    skipBlurRef.current = false;
    setDraft(title); setError(null); setEditing(true);
    requestAnimationFrame(() => inputRef.current?.select());
  };
  const commit = async () => {
    const next = draft.replace(/\s+/g, " ").trim();
    if (!next || next === title) { setEditing(false); return; }
    setSaving(true);
    try { await onRename(next); setEditing(false); setError(null); }
    catch (err) { setError(err.message || "Couldn't rename this conversation."); }
    finally { setSaving(false); }
  };

  return (
    <div className="conv-head">
      {editing ? (
        <input
          ref={inputRef}
          className="conv-edit"
          value={draft}
          maxLength={80}
          disabled={saving}
          aria-label="Conversation name"
          aria-invalid={error ? true : undefined}
          onChange={e => setDraft(e.target.value)}
          onKeyDown={e => {
            if (e.key === "Enter") { e.preventDefault(); skipBlurRef.current = true; commit(); }
            if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); skipBlurRef.current = true; setEditing(false); setError(null); }
          }}
          onBlur={() => { if (skipBlurRef.current) { skipBlurRef.current = false; return; } if (!saving) commit(); }}
        />
      ) : (
        <h1 className="conv-title" title={title}>{title}</h1>
      )}
      {!editing && (
        <button type="button" className="icon-btn small" onClick={start} aria-label="Rename conversation" title="Rename">
          <PencilIcon size={15} />
        </button>
      )}
      {error && <span className="conv-error" role="alert">{error}</span>}
      <span className="conv-count">{count} {count === 1 ? "message" : "messages"}</span>
      <button type="button" className="icon-btn bordered" onClick={onDelete} aria-label="Delete conversation" title="Delete conversation">
        <TrashIcon size={15} />
      </button>
    </div>
  );
}

// ═══════════════════════════════════════════════════════
// WELCOME / EMPTY STATE
// ═══════════════════════════════════════════════════════
// Starters put an editable prompt in the composer — they never send on their
// own. The trailing text is left open for the user to finish.
const STARTERS = [
  { icon: SearchIcon, title: "Research a topic",  prompt: "Research the latest developments in ", tone: "indigo" },
  { icon: CodeIcon,   title: "Help me with code", prompt: "Help me with this code:\n\n",          tone: "cyan" },
  { icon: BookIcon,   title: "Explain a concept", prompt: "Explain in simple terms how ",         tone: "indigo" },
  { icon: SparkIcon,  title: "Help me plan",      prompt: "Help me plan ",                        tone: "cyan" },
];

function greeting() {
  const h = new Date().getHours();
  return h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
}

function EmptyState({ composer, onPrompt, recent, onOpen, onTools }) {
  return (
    <div className="hero">
      <div className="hero-inner">
        <span className="hero-mark"><BrandMark size={58} /></span>
        <div className="eyebrow">{greeting()}</div>
        <h1 className="hero-title">What can we <span className="hero-accent">explore</span> today?</h1>
        <p className="hero-sub">Ask anything. Connect ideas. Get things done.</p>

        <div className="hero-composer">{composer}</div>

        <div className="starter-row">
          {STARTERS.map(({ icon: Icon, title, prompt, tone }) => (
            <button type="button" className={`starter-chip ${tone}`} key={title} onClick={() => onPrompt(prompt)}>
              <Icon size={15} />
              {title}
            </button>
          ))}
        </div>

        {recent.length > 0 && (
          <section className="continue" aria-label="Continue where you left off">
            <div className="continue-head">
              <span className="side-label flat">Continue where you left off</span>
              <button type="button" className="link-btn plain" onClick={onTools}>
                {TOOLS.length} tools connected <ArrowRightIcon size={13} />
              </button>
            </div>
            <div className="continue-grid">
              {recent.map(r => (
                <button type="button" className="continue-card" key={r.tid} onClick={() => onOpen(r.tid)}>
                  <ChatIcon size={16} />
                  <span className="continue-title">{r.title}</span>
                  <span className="continue-meta">{r.count ? `${r.count} messages` : "Open conversation"}</span>
                </button>
              ))}
            </div>
          </section>
        )}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════
// AGENT ACTIVITY
// ═══════════════════════════════════════════════════════
const secs = ms => (ms / 1000).toFixed(1);

// Live card while an answer is being produced. Steps appear only when the
// backend reports them; nothing is predicted. Ticks its own timer so the
// rest of the chat doesn't re-render ten times a second.
function LiveActivity({ activity }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 100);
    return () => clearInterval(id);
  }, []);
  const { startedAt, steps, writing } = activity;
  const head = writing ? "Writing the answer…" : steps.length ? "Working on it" : "Thinking…";
  return (
    <div className="activity live">
      <div className="activity-head">
        <SparkIcon size={15} />
        <span className="activity-title">{head}</span>
        <span className="activity-time">{secs(now - startedAt)}s</span>
      </div>
      {steps.length > 0 && (
        <ol className="activity-steps">
          {steps.map((s, i) => {
            const label = STEP_LABELS[s.key] || { live: s.key, done: s.key };
            return (
              <li key={i} className={s.end ? "done" : "current"}>
                {s.end ? <CheckIcon size={15} /> : <span className="spinner small" aria-hidden="true" />}
                <span>{s.end ? label.done : label.live}</span>
                {s.end && <span className="activity-time">{secs(s.end - s.start)}s</span>}
              </li>
            );
          })}
        </ol>
      )}
      <div className="activity-bar" aria-hidden="true"><span /></div>
    </div>
  );
}

// Finished answer: collapses to "Worked for 6.2s · 4 steps".
function ActivitySummary({ activity }) {
  const [open, setOpen] = useState(false);
  if (!activity?.steps?.length) return null;
  const n = activity.steps.length;
  return (
    <div className={`activity summary ${open ? "open" : ""}`}>
      <button type="button" className="activity-toggle" aria-expanded={open} onClick={() => setOpen(o => !o)}>
        <SparkIcon size={15} />
        <span className="activity-title">Worked for {activity.total.toFixed(1)}s</span>
        <span className="activity-meta">· {n} {n === 1 ? "step" : "steps"}</span>
        <ChevronDownIcon size={14} />
      </button>
      {open && (
        <ol className="activity-steps">
          {activity.steps.map((s, i) => (
            <li key={i} className="done">
              <CheckIcon size={15} />
              <span>{(STEP_LABELS[s.key] || { done: s.key }).done}</span>
              <span className="activity-time">{s.secs.toFixed(1)}s</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════
// MESSAGE CONTENT
// ═══════════════════════════════════════════════════════
function CodeBlock({ language, code, highlight }) {
  const [copied, setCopied] = useState(null); // null | "ok" | "err"
  const timerRef = useRef(null);
  useEffect(() => () => clearTimeout(timerRef.current), []);

  const copy = async () => {
    try { await navigator.clipboard.writeText(code); setCopied("ok"); }
    catch { setCopied("err"); }
    clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => setCopied(null), 1800);
  };

  return (
    <div className="code-block">
      <div className="code-head">
        <span className="code-lang">{language || "text"}</span>
        <button type="button" className="code-copy" onClick={copy} aria-label="Copy code">
          {copied === "ok" ? <><CheckIcon size={13} /> Copied</> : copied === "err" ? "Copy failed" : <><CopyIcon size={13} /> Copy</>}
        </button>
      </div>
      {highlight && language
        ? <SyntaxHighlighter style={oneDark} language={language} PreTag="div" className="code-body"
            customStyle={{ margin: 0, background: "transparent", padding: "14px 16px", fontSize: "13px", lineHeight: 1.6 }}
            codeTagProps={{ style: { fontFamily: "var(--mono)" } }}>
            {code}
          </SyntaxHighlighter>
        : <pre className="code-body code-plain"><code>{code}</code></pre>}
    </div>
  );
}

// react-markdown v9 renders fenced code as <pre><code class="language-x">.
// We take over <pre> to add the language label + copy button. While an
// answer streams, code renders as plain text (no Prism tokenizing) so
// scrolling stays smooth; same box, font and size, so nothing shifts.
function makePre(highlight) {
  return function Pre({ children }) {
    const codeEl = React.Children.toArray(children)[0];
    const className = codeEl?.props?.className || "";
    const match = /language-([\w+#-]+)/.exec(className);
    const code = String(codeEl?.props?.children ?? "").replace(/\n$/, "");
    return <CodeBlock language={match ? match[1] : ""} code={code} highlight={highlight} />;
  };
}

const MdLink = ({ node, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer" />;
const MdTable = ({ node, ...props }) => (
  <div className="table-wrap" tabIndex={0} role="region" aria-label="Table">
    <table {...props} />
  </div>
);

const REMARK_PLUGINS = [remarkGfm];
const MARKDOWN_COMPONENTS = { pre: makePre(true), a: MdLink, table: MdTable };
const STREAMING_MARKDOWN_COMPONENTS = { pre: makePre(false), a: MdLink, table: MdTable };

// Sources = the links inside the answer itself (markdown links and bare
// URLs), one chip per site. Nothing is fetched or invented.
const MD_LINK_RE = /\[[^\]]*\]\((https?:\/\/[^\s)]+)\)/g;
const BARE_URL_RE = /https?:\/\/[^\s<>()[\]"'`]+/g;
const SOURCE_TONES = ["cyan", "amber", "indigo", "green"];

function extractSources(text) {
  if (!text) return [];
  const urls = [];
  const rest = text.replace(MD_LINK_RE, (m, url) => { urls.push(url); return " "; });
  for (const m of rest.match(BARE_URL_RE) || []) urls.push(m.replace(/[.,;:!?]+$/, ""));
  const seen = new Map();
  for (const url of urls) {
    try {
      const host = new URL(url).hostname.replace(/^www\./, "");
      if (!seen.has(host)) seen.set(host, url);
    } catch { /* not a URL */ }
  }
  return [...seen.entries()].slice(0, 6).map(([host, url]) => {
    let h = 0; for (const c of host) h = (h * 31 + c.charCodeAt(0)) >>> 0;
    return { host, url, tone: SOURCE_TONES[h % SOURCE_TONES.length] };
  });
}

function Sources({ content }) {
  const sources = useMemo(() => extractSources(content), [content]);
  if (!sources.length) return null;
  return (
    <div className="sources">
      <span className="sources-label">Sources</span>
      {sources.map(s => (
        <a key={s.host} className="source-chip" href={s.url} target="_blank" rel="noopener noreferrer" title={s.url}>
          <span className={`source-dot ${s.tone}`} aria-hidden="true">{s.host[0].toUpperCase()}</span>
          {s.host}
        </a>
      ))}
    </div>
  );
}

function ErrorMessage({ content, restored }) {
  const detail = content.slice(ERROR_PREFIX.length).trim();
  return (
    <div className="inline-alert error" role="alert">
      <AlertIcon size={16} />
      <div>
        <div className="alert-title">Something went wrong while generating a response.</div>
        {detail && <div className="alert-detail">{detail}</div>}
        {restored && <div className="alert-detail">Your message is back in the composer — press Send to try again.</div>}
      </div>
    </div>
  );
}

function MessageActions({ content, activity, reading, canAskAgain, onReadAloud, onAskAgain }) {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef(null);
  useEffect(() => () => clearTimeout(timerRef.current), []);
  const copy = async () => {
    try { await navigator.clipboard.writeText(content); setCopied(true); } catch { setCopied(false); }
    clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => setCopied(false), 1600);
  };
  return (
    <div className="msg-actions">
      <button type="button" className="act-btn" onClick={copy} aria-label={copied ? "Copied" : "Copy answer"}>
        {copied ? <CheckIcon size={14} /> : <CopyIcon size={14} />}<span>{copied ? "Copied" : "Copy"}</span>
      </button>
      <button type="button" className={`act-btn ${reading ? "on" : ""}`} onClick={onReadAloud} aria-pressed={reading} aria-label={reading ? "Stop reading" : "Read aloud"}>
        {reading ? <StopIcon size={13} /> : <VolumeIcon size={14} />}<span>{reading ? "Stop reading" : "Read aloud"}</span>
      </button>
      {canAskAgain && (
        <button type="button" className="act-btn" onClick={onAskAgain} aria-label="Ask again">
          <RefreshIcon size={14} /><span>Ask again</span>
        </button>
      )}
      {activity?.total != null && <span className="act-meta">{activity.total.toFixed(1)}s</span>}
    </div>
  );
}

// memo: while an answer streams, ChatApp re-renders every frame — without
// this, every earlier message re-parsed its markdown and re-highlighted its
// code each time. Callbacks passed in are stable (see ChatApp).
const Message = React.memo(function Message({
  index, role, content, streaming, attachmentName, restored, activity,
  isLast, reading, busy, canAskAgain, onReadAloud, onAskAgain,
}) {
  if (role === "user") {
    return (
      <div className="msg msg-user">
        <div className="user-bubble">
          {attachmentName && <div className="msg-attachment"><PaperclipIcon size={13} /> {attachmentName}</div>}
          <span className="user-text">{content}</span>
        </div>
      </div>
    );
  }
  const isError = typeof content === "string" && content.startsWith(ERROR_PREFIX);
  return (
    <div className={`msg msg-ai ${isLast ? "is-last" : ""}`}>
      <BrandMark size={30} />
      <div className="ai-body">
        {isError
          ? <ErrorMessage content={content} restored={restored} />
          : <>
              {!streaming && <ActivitySummary activity={activity} />}
              <div className="markdown">
                <ReactMarkdown remarkPlugins={REMARK_PLUGINS} components={streaming ? STREAMING_MARKDOWN_COMPONENTS : MARKDOWN_COMPONENTS}>
                  {content}
                </ReactMarkdown>
                {streaming && <span className="cursor" aria-hidden="true" />}
              </div>
              {!streaming && <Sources content={content} />}
              {!streaming && (
                <MessageActions
                  content={content}
                  activity={activity}
                  reading={reading}
                  canAskAgain={canAskAgain && !busy}
                  onReadAloud={() => onReadAloud(index)}
                  onAskAgain={() => onAskAgain(index)}
                />
              )}
            </>}
      </div>
    </div>
  );
});

// ═══════════════════════════════════════════════════════
// COMPOSER
// ═══════════════════════════════════════════════════════
function VoiceStatusBar({ voiceMode, listening, speaking, onStop, voiceOptions, selectedVoice, onVoiceChange }) {
  if (!voiceMode) return null;
  let label;
  if (listening) label = "Listening…";
  else if (speaking) label = "Speaking… tap the mic to interrupt";
  else label = "Standing by — listening again shortly after the reply";
  return (
    <div className="voice-status-bar" role="status">
      <span className={`voice-pulse ${listening ? "listen" : speaking ? "speak" : ""}`} aria-hidden="true" />
      <span className="voice-status-text">{label}</span>
      <label className="sr-only" htmlFor="voice-select">Voice</label>
      <select
        id="voice-select"
        className="voice-select"
        value={selectedVoice}
        onChange={e => onVoiceChange(e.target.value)}
        title="Voice"
      >
        <option value="browser">Browser voice</option>
        {voiceOptions.map(v => (
          <option key={v.id} value={v.id}>{v.label}</option>
        ))}
      </select>
      <button type="button" className="btn-ghost small" onClick={onStop}>Stop voice</button>
    </div>
  );
}

function Composer({
  hero = false, placeholder,
  value, onChange, onSend, disabled,
  voiceSupported, voiceMode, listening, speaking,
  onVoiceToggle, inputRef,
  attachment, uploading, uploadError,
  onAttachClick, onRemoveAttachment, fileInputRef, onFileSelect,
  onStop, onTools,
}) {
  useEffect(() => {
    const el = inputRef.current;
    if (el) { el.style.height = "auto"; el.style.height = `${el.scrollHeight}px`; }
  }, [value, inputRef]);
  const canSend = (value.trim() || attachment) && !disabled && !uploading;
  const submit = () => { if (canSend) onSend(value, { fromVoice: false }); };
  return (
    <div className={`composer-zone ${hero ? "is-hero" : ""}`}>
      {attachment && (
        <div className="attachment-chip">
          <span className="attachment-icon">{attachment.kind === "image" ? <ImageIcon size={15} /> : <FileIcon size={15} />}</span>
          <span className="attachment-name">{attachment.filename}</span>
          <button type="button" className="attachment-remove" onClick={onRemoveAttachment} aria-label={`Remove attachment ${attachment.filename}`}>
            <CloseIcon size={14} />
          </button>
        </div>
      )}
      {uploading && (
        <div className="attachment-chip attachment-loading" role="status">
          <span className="spinner small" aria-hidden="true" /> Reading file…
        </div>
      )}
      {uploadError && <div className="inline-alert error composer-alert" role="alert"><AlertIcon size={15} /> {uploadError}</div>}

      <div className={`composer-shell ${hero ? "is-hero" : ""} ${voiceMode ? "voice-active" : ""} ${disabled ? "is-busy" : ""}`}>
        <div className="composer" onClick={e => { if (e.target === e.currentTarget) inputRef.current?.focus(); }}>
          <input type="file" ref={fileInputRef} onChange={onFileSelect} accept=".pdf,.docx,image/*" hidden />
          <label className="sr-only" htmlFor="composer-input">Message SynapseAI</label>
          <textarea
            id="composer-input"
            ref={inputRef}
            rows={hero ? 2 : 1}
            className="input-field"
            placeholder={voiceMode ? "Voice mode — speak or type a message…" : (disabled ? "SynapseAI is answering…" : placeholder)}
            value={value}
            disabled={disabled && !voiceMode}
            onChange={e => onChange(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } }}
            aria-describedby="composer-hint"
          />
          <div className="composer-bar">
            <button type="button" className="chip-btn" onClick={onAttachClick} disabled={disabled || uploading} title="Attach a PDF, DOCX or image" aria-label="Attach a PDF, DOCX or image">
              <PaperclipIcon size={15} /><span>Attach</span>
            </button>
            {voiceSupported && (
              <button
                type="button"
                className={`chip-btn mic-btn ${voiceMode ? "on" : ""} ${listening ? "listen" : ""} ${speaking ? "speak" : ""}`}
                onClick={onVoiceToggle}
                disabled={disabled && !voiceMode}
                title={voiceMode ? "Exit voice mode" : "Start voice chat (same tools and memory)"}
                aria-label={voiceMode ? "Exit voice mode" : "Start voice chat"}
                aria-pressed={voiceMode}
              >
                <MicIcon size={15} /><span>Voice</span>
              </button>
            )}
            <button type="button" className="chip-btn" onClick={onTools} disabled={disabled} title="See the tools SynapseAI can use">
              <ToolsIcon size={15} /><span>Tools</span>
            </button>
            <span className="composer-keys">{disabled ? "Press Stop to keep what's written so far" : "Enter to send · Shift+Enter for a new line"}</span>
            {disabled
              ? <button type="button" className="send-btn stop-btn" onClick={onStop} title="Stop generating" aria-label="Stop generating">
                  <StopIcon size={14} /><span>Stop</span>
                </button>
              : <button type="button" className="send-btn" onClick={submit} disabled={!canSend} aria-label="Send message" title="Send (Enter)">
                  <SendIcon size={17} />
                </button>
            }
          </div>
        </div>
      </div>
      <p className="composer-hint" id="composer-hint">AI-generated responses may be inaccurate. Verify important information.</p>
    </div>
  );
}

// ═══════════════════════════════════════════════════════
// ROOT APP
// ═══════════════════════════════════════════════════════
function ChatApp() {
  const [threadId,    setThreadId]    = useState(uuid);
  const [messages,    setMessages]    = useState([]);
  const [threads,     setThreads]     = useState([]);
  const [titles,      setTitles]      = useState({});
  const titlesRef = useRef(titles);
  titlesRef.current = titles;
  const [streaming,   setStreaming]   = useState(false);
  const [streamMsg,   setStreamMsg]   = useState("");
  const [activity,    setActivity]    = useState(null);         // live agent steps
  const [input,       setInput]       = useState("");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [collapsed,   setCollapsed]   = useState(() => readPref("synapse_sidebar", "open") === "collapsed");
  const [view,        setView]        = useState("assistant"); // "assistant" | "tools"
  const [historyStatus, setHistoryStatus] = useState("loading"); // loading | ready | error
  const [historyReload, setHistoryReload] = useState(0);
  const [health,      setHealth]      = useState("checking");
  const [announce,    setAnnounce]    = useState("");          // screen-reader live region
  const [showJump,    setShowJump]    = useState(false);
  const [readingIdx,  setReadingIdx]  = useState(null);
  const [voiceMode,   setVoiceMode]   = useState(false);
  const [listening,   setListening]   = useState(false);
  const [speaking,    setSpeaking]    = useState(false);
  const [voiceError,  setVoiceError]  = useState(null);
  const [voiceOptions,  setVoiceOptions]  = useState([]);
  const [selectedVoice, setSelectedVoice] = useState(() => readPref("synapse_voice", "browser"));

  const [attachment,  setAttachment]  = useState(null);   // { filename, kind, content }
  const [uploading,   setUploading]   = useState(false);
  const [uploadError, setUploadError] = useState(null);
  const fileInputRef = useRef(null);

  const isMobile = useMediaQuery("(max-width: 767px)");
  const voiceRef = useRef(null);
  const inputElRef = useRef(null);
  const navBtnRef = useRef(null);
  const sidebarOpenRef = useRef(false);
  sidebarOpenRef.current = sidebarOpen;
  const voiceSupported = isVoiceSupported();

  // threadMessages stores the FULL message history per thread locally in the
  // frontend — independent from backend state. The backend can trim messages
  // for memory management, but the UI always shows the complete conversation.
  const [threadMessages, setThreadMessages] = useState({});
  const messagesRef = useRef(messages);
  messagesRef.current = messages;

  const streamRef = useRef("");
  const scrollerRef = useRef(null);
  // Auto-follow new content only while the user is at the bottom — scrolling
  // up to read mid-stream pauses it; scrolling back down resumes it.
  const followRef = useRef(true);
  const lastAutoTopRef = useRef(null);   // scrollTop we set ourselves
  const prevTopRef = useRef(0);
  const autoUntilRef = useRef(0);        // ignore scroll events during our smooth scroll
  const showJumpRef = useRef(false);

  const say = useCallback(msg => {
    // Toggle a zero-width char so repeating the same text is still announced.
    setAnnounce(prev => (prev === msg ? `${msg}​` : msg));
  }, []);

  const setJump = useCallback(v => {
    if (showJumpRef.current !== v) { showJumpRef.current = v; setShowJump(v); }
  }, []);

  // Load threads (retry while backend warms up — embeddings can take 15–30s)
  useEffect(() => {
    let cancelled = false;
    let retryTimer = null;
    setHistoryStatus("loading");

    const loadThreads = (attempt = 0) => {
      api.getThreads()
        .then(({ threads: ids, titles: stored }) => {
          if (cancelled) return;
          // Keep any conversation started locally while history was loading.
          setThreads(p => [...ids, ...p.filter(t => !ids.includes(t))]);
          // Stored titles show immediately; history fills in the rest.
          setTitles(p => ({ ...p, ...stored }));
          setHistoryStatus("ready");
          ids.forEach(tid =>
            api.getHistory(tid).then(h => {
              if (cancelled) return;
              // Don't replace a real title (stored, or renamed meanwhile).
              setTitles(p => (p[tid] && p[tid] !== "New Conversation")
                ? p
                : { ...p, [tid]: h.title || "New Conversation" });
              setThreadMessages(p => {
                if (p[tid]) return p;
                return { ...p, [tid]: h.messages || [] };
              });
            }).catch(() => { /* history optional on startup */ })
          );
        })
        .catch(() => {
          if (cancelled) return;
          if (attempt >= 12) { setHistoryStatus("error"); return; }
          retryTimer = setTimeout(() => loadThreads(attempt + 1), 2000);
        });
    };

    loadThreads();
    return () => {
      cancelled = true;
      if (retryTimer) clearTimeout(retryTimer);
    };
  }, [historyReload]);

  // Backend status for the top bar — retried while the Space wakes up.
  useEffect(() => {
    let cancelled = false;
    let timer = null;
    const check = (attempt = 0) => {
      fetch(`${BASE}/health`)
        .then(r => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
        .then(d => { if (!cancelled) setHealth(d.ok ? "ok" : "degraded"); })
        .catch(() => {
          if (cancelled) return;
          if (attempt < 6) timer = setTimeout(() => check(attempt + 1), 5000);
          else setHealth("down");
        });
    };
    check();
    return () => { cancelled = true; clearTimeout(timer); };
  }, []);

  // Auto-scroll. One rAF loop eases toward the bottom while streaming, and a
  // single smooth scroll runs when a message is added (per-token
  // scrollIntoView restarted animations mid-flight and jittered).
  const handleMessagesScroll = useCallback(() => {
    const el = scrollerRef.current;
    if (!el) return;
    const top = el.scrollTop;
    const prevTop = prevTopRef.current;
    prevTopRef.current = top;
    const distance = el.scrollHeight - el.clientHeight - top;
    setJump(distance > 240);
    if (Date.now() < autoUntilRef.current) return;
    if (lastAutoTopRef.current !== null && Math.abs(top - lastAutoTopRef.current) < 2) return;
    // Resume when back at the bottom. "Near + moving down" counts too: during a
    // fast stream new content lands before this event fires.
    if (distance < 8 || (top > prevTop && distance < 120)) followRef.current = true;
    else if (top < prevTop - 2 && distance > 80) followRef.current = false;   // moved up, e.g. scrollbar drag
  }, [setJump]);

  // Trackpads scroll in tiny steps that never cross the 80px line, so read
  // the intent directly: any upward wheel/touch scroll pauses auto-follow.
  const pauseFollowOnWheel = useCallback(e => { if (e.deltaY < 0) followRef.current = false; }, []);
  const pauseFollowOnTouch = useCallback(() => { followRef.current = false; }, []);

  const jumpToLatest = useCallback(() => {
    const el = scrollerRef.current;
    if (!el) return;
    followRef.current = true;
    autoUntilRef.current = Date.now() + 600;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    setJump(false);
  }, [setJump]);

  useEffect(() => {
    const el = scrollerRef.current;
    if (!el || !followRef.current) return;
    autoUntilRef.current = Date.now() + 600;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    if (!streaming) return;
    let raf;
    const tick = () => {
      const el = scrollerRef.current;
      if (el && followRef.current && Date.now() >= autoUntilRef.current) {
        const gap = el.scrollHeight - el.clientHeight - el.scrollTop;
        if (gap > 1) {
          el.scrollTop += Math.max(1, gap * 0.2);   // ease in: big chunks glide, small ones barely move
          lastAutoTopRef.current = el.scrollTop;
        }
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [streaming]);

  useEffect(() => {
    fetch(`${BASE}/voice/options`)
      .then(r => r.ok ? r.json() : { voices: [] })
      .then(d => setVoiceOptions(d.voices || []))
      .catch(() => {}); // fine — browser voice still works
  }, []);

  useEffect(() => {
    setActiveVoice(selectedVoice);
    writePref("synapse_voice", selectedVoice);
  }, [selectedVoice]);

  useEffect(() => {
    voiceRef.current = createVoiceSession({
      onTranscript: (text) => {
        setInput(text);
        sendRef.current?.(text, { fromVoice: true });
      },
      onListeningChange: setListening,
      onSpeakingChange: setSpeaking,
      onError: (msg) => setVoiceError(msg),
    });
    return () => voiceRef.current?.exitVoiceMode();
  }, []);

  // Stop any "Read aloud" when leaving the chat screen.
  useEffect(() => () => stopReadAloud(), []);

  const sendRef = useRef(null);
  const abortRef = useRef(null);          // AbortController of the in-flight answer
  const stopRequestedRef = useRef(false); // distinguishes Stop from a voice interruption

  // Mobile drawer close — returns focus to the menu button that opened it.
  const closeSidebar = useCallback(() => {
    if (!sidebarOpenRef.current) return;
    setSidebarOpen(false);
    requestAnimationFrame(() => navBtnRef.current?.focus());
  }, []);

  const toggleNav = useCallback(() => {
    if (isMobile) { setSidebarOpen(true); return; }
    setCollapsed(c => { writePref("synapse_sidebar", c ? "open" : "collapsed"); return !c; });
  }, [isMobile]);

  const focusComposer = useCallback((caretToEnd = false) => {
    requestAnimationFrame(() => {
      const el = inputElRef.current;
      if (!el) return;
      el.focus();
      if (caretToEnd) el.setSelectionRange(el.value.length, el.value.length);
    });
  }, []);

  const stopReading = useCallback(() => { stopReadAloud(); setReadingIdx(null); }, []);

  const loadThread = useCallback((tid) => {
    // Never re-fetch from backend when switching threads — use the local
    // threadMessages copy, which has the full history.
    setView("assistant");
    if (tid === threadId) return;
    stopReading();
    followRef.current = true;
    setJump(false);
    setThreadId(tid);
    setMessages(threadMessages[tid] || []);
  }, [threadId, threadMessages, stopReading, setJump]);

  const newThread = useCallback(async () => {
    stopReading();
    const tid = await api.newThread();
    setThreadId(tid);
    setMessages([]);
    setView("assistant");
    setJump(false);
    setThreads(p => [...p, tid]);
    setTitles(p => ({ ...p, [tid]: "New Conversation" }));
    setThreadMessages(p => ({ ...p, [tid]: [] }));
    closeSidebar();
    focusComposer();
  }, [closeSidebar, focusComposer, stopReading, setJump]);

  // Ctrl/Cmd+K → new conversation (shown on the New conversation button).
  const newThreadRef = useRef(newThread);
  newThreadRef.current = newThread;
  useEffect(() => {
    const onKey = e => {
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "k") {
        e.preventDefault();
        newThreadRef.current?.();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const removeThread = useCallback(async (tid) => {
    await api.deleteThread(tid);
    setThreads(p => p.filter(t => t !== tid));
    setTitles(p => { const n = { ...p }; delete n[tid]; return n; });
    setThreadMessages(p => { const n = { ...p }; delete n[tid]; return n; });
    if (tid === threadId) { stopReading(); setThreadId(uuid()); setMessages([]); }
  }, [threadId, stopReading]);

  // Optimistic rename; restores the previous title if the server refuses.
  const renameThread = useCallback(async (tid, title) => {
    const previous = titlesRef.current[tid];
    setTitles(p => ({ ...p, [tid]: title }));
    try {
      const saved = await api.renameThread(tid, title);
      setTitles(p => ({ ...p, [tid]: saved || title }));
      say("Conversation renamed.");
    } catch (err) {
      setTitles(p => ({ ...p, [tid]: previous }));
      throw err;
    }
  }, [say]);

  const send = useCallback(async (text, options = {}) => {
    const { fromVoice = false } = options;
    const currentAttachment = fromVoice ? null : attachment; // voice mode stays text-only
    if ((!text.trim() && !currentAttachment) || streaming) return;

    stopReading();
    voiceRef.current?.interrupt();
    // One controller per answer: Stop aborts it; in voice mode, the voice
    // session's own interrupt signal (user starts speaking) aborts it too.
    const controller = new AbortController();
    const voiceSignal = fromVoice ? voiceRef.current?.newAbortSignal() : undefined;
    voiceSignal?.addEventListener("abort", () => controller.abort());
    const signal = controller.signal;
    abortRef.current = controller;
    stopRequestedRef.current = false;

    setInput("");
    setVoiceError(null);
    if (currentAttachment) setAttachment(null);

    if (!threads.includes(threadId)) setThreads(p => [...p, threadId]);

    followRef.current = true;   // sending always brings the conversation back into view

    // Append the user message both to display state AND the local thread store
    const displayText = text.trim() || `Sent ${currentAttachment?.filename}`;
    const userMsg = { role: "user", content: displayText, attachmentName: currentAttachment?.filename };
    setMessages(p => {
      const updated = [...p, userMsg];
      setThreadMessages(prev => ({ ...prev, [threadId]: updated }));
      return updated;
    });

    // The model sees the extracted file content too; the chat bubble above stays clean.
    const modelText = currentAttachment
      ? `${text.trim() ? text.trim() + "\n\n" : ""}[Attached ${currentAttachment.kind}: ${currentAttachment.filename}]\n"""\n${currentAttachment.content}\n"""`
      : text;

    // Auto-title on the first message. The backend stores it (short messages
    // become their own title, no LLM call) and never replaces a rename.
    const isFirst = messages.filter(m => m.role === "user").length === 0;
    if (isFirst) {
      const tid = threadId;
      api.getSummary(displayText, tid)
        .then(t => setTitles(p => ({ ...p, [tid]: t })))
        .catch(() => { /* title stays "New conversation"; history retries it */ });
    }

    // Agent activity: steps open/close as status events arrive; the first
    // answer token closes the last step ("Writing the answer…").
    const act = { startedAt: Date.now(), steps: [], writing: false };
    const publishActivity = () => setActivity({ ...act, steps: act.steps.map(s => ({ ...s })) });
    const closeStep = (now) => { const last = act.steps[act.steps.length - 1]; if (last && !last.end) last.end = now; };
    publishActivity();

    setStreaming(true); streamRef.current = ""; setStreamMsg("");

    let spokenUpTo = 0;
    const ttsBufferRef = { current: "" };
    // Render at most once per frame — fast streams deliver many tokens per frame.
    let renderFrame = null;
    const renderStream = () => { renderFrame = null; setStreamMsg(streamRef.current); };

    const finishActivity = () => {
      const now = Date.now();
      closeStep(now);
      return {
        total: (now - act.startedAt) / 1000,
        steps: act.steps.map(s => ({ key: s.key, secs: ((s.end || now) - s.start) / 1000 })),
      };
    };

    try {
      const full = await api.streamChat(
        threadId,
        modelText,
        token => {
          if (!act.writing) { act.writing = true; closeStep(Date.now()); publishActivity(); }
          streamRef.current += token;
          if (renderFrame === null) renderFrame = requestAnimationFrame(renderStream);

          if (fromVoice && voiceRef.current) {
            ttsBufferRef.current += token;
            const { chunks, newSpokenUpTo } = extractNewSpeechChunks(
              ttsBufferRef.current,
              spokenUpTo
            );
            spokenUpTo = newSpokenUpTo;
            for (const s of chunks) voiceRef.current.enqueueSpeech(s);
          }
        },
        status => {
          if (!STEP_LABELS[status]) return;
          const last = act.steps[act.steps.length - 1];
          if (last && last.key === status && !last.end) return;
          const now = Date.now();
          closeStep(now);
          act.steps.push({ key: status, start: now, end: null });
          act.writing = false;
          publishActivity();
          if (fromVoice && status === "using_tools") voiceRef.current?.speakStatus("One moment.");
        },
        { signal, voice: fromVoice }
      );

      if (fromVoice && voiceRef.current && full) {
        const tail = ttsBufferRef.current.slice(spokenUpTo).trim();
        if (tail.length >= 8) voiceRef.current.enqueueSpeech(tail);
      }

      // An empty answer is a recoverable error, not a blank bubble.
      if (!full) throw new Error("The assistant didn't return a response.");

      const assistantMsg = { role: "assistant", content: full, activity: finishActivity() };
      setMessages(p => {
        const updated = [...p, assistantMsg];
        setThreadMessages(prev => ({ ...prev, [threadId]: updated }));
        return updated;
      });
      say("Response complete.");
    } catch (err) {
      if (err.name === "AbortError") {
        // Stop: keep whatever was already shown. (A voice interruption
        // discards it — the user is already saying something new.)
        const partial = streamRef.current;
        if (stopRequestedRef.current && partial.trim()) {
          const stopped = { role: "assistant", content: partial, activity: finishActivity() };
          setMessages(p => {
            const updated = [...p, stopped];
            setThreadMessages(prev => ({ ...prev, [threadId]: updated }));
            return updated;
          });
        }
        if (stopRequestedRef.current) say("Generation stopped.");
        return;
      }
      // Keep the user's input: put the text — and attachment — back in the
      // composer so retrying is one click on Send.
      const restored = !fromVoice;
      if (restored) {
        setInput(cur => cur || text);
        if (currentAttachment) setAttachment(a => a || currentAttachment);
      }
      const errMsg = { role: "assistant", content: `${ERROR_PREFIX}${err.message}`, restored };
      setMessages(p => {
        const updated = [...p, errMsg];
        setThreadMessages(prev => ({ ...prev, [threadId]: updated }));
        return updated;
      });
      say("The response failed.");
      if (fromVoice) voiceRef.current?.speakStatus("Sorry, something went wrong.");
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      if (renderFrame !== null) cancelAnimationFrame(renderFrame);
      setStreaming(false); setStreamMsg(""); setActivity(null); streamRef.current = "";
      if (fromVoice) voiceRef.current?.notifyAssistantTurnComplete();
      if (!fromVoice) {
        // Re-focus the input — a disabled→enabled textarea doesn't regain
        // browser focus automatically.
        requestAnimationFrame(() => inputElRef.current?.focus());
      }
    }
  }, [streaming, messages, threadId, threads, attachment, say, stopReading]);

  sendRef.current = send;

  // Stop generating: the UI stops at once (abort), and the backend is told
  // explicitly too — proxies (Vercel/HF) don't always pass a disconnect on, and
  // without it the model and tools would keep running and burning quota.
  const stopGeneration = useCallback(() => {
    if (!abortRef.current) return;
    stopRequestedRef.current = true;
    api.stopChat(threadId).catch(() => { /* the abort below still ends it locally */ });
    abortRef.current.abort();
  }, [threadId]);

  // Message actions — stable callbacks (read state through refs) so memoized
  // messages don't re-render on every streaming frame.
  const handleReadAloud = useCallback((i) => {
    if (readingIdx === i) { stopReading(); return; }
    const msg = messagesRef.current[i];
    if (!msg) return;
    setReadingIdx(i);
    readAloud(msg.content, {
      onDone: () => setReadingIdx(cur => (cur === i ? null : cur)),
      onNotice: (m) => setVoiceError(m),
    });
  }, [readingIdx, stopReading]);
  const readAloudRef = useRef(handleReadAloud);
  readAloudRef.current = handleReadAloud;
  const onReadAloud = useCallback(i => readAloudRef.current(i), []);

  const onAskAgain = useCallback((i) => {
    const list = messagesRef.current;
    for (let j = i - 1; j >= 0; j--) {
      if (list[j].role === "user") { sendRef.current?.(list[j].content, { fromVoice: false }); return; }
    }
  }, []);

  const handleVoiceToggle = useCallback(() => {
    if (!voiceSupported) {
      setVoiceError("Use Chrome or Edge for voice chat.");
      return;
    }
    const v = voiceRef.current;
    if (!v) return;
    stopReading();
    if (v.isVoiceMode()) {
      v.exitVoiceMode();
      setVoiceMode(false);
    } else {
      v.enterVoiceMode();
      setVoiceMode(true);
      setVoiceError(null);
    }
  }, [voiceSupported, stopReading]);

  const handleStopVoice = useCallback(() => {
    voiceRef.current?.exitVoiceMode();
    setVoiceMode(false);
    setListening(false);
    setSpeaking(false);
  }, []);

  const handleFileSelect = useCallback(async (e) => {
    const file = e.target.files?.[0];
    e.target.value = ""; // allow re-selecting the same file later
    if (!file) return;
    setUploadError(null);
    setUploading(true);
    try {
      const result = await api.uploadFile(file);
      // Attached only after the server has read the file.
      setAttachment({ filename: file.name, kind: result.kind, content: result.content });
    } catch (err) {
      setUploadError(err.message || "Upload failed");
    } finally {
      setUploading(false);
    }
  }, []);

  const removeAttachment = useCallback(() => setAttachment(null), []);

  // Prompt starters and tool cards fill the composer; the user sends.
  const fillComposer = useCallback((text) => {
    setView("assistant");
    setInput(text);
    closeSidebar();
    focusComposer(true);
  }, [closeSidebar, focusComposer]);

  const changeView = useCallback((v) => {
    setView(v);
    closeSidebar();
  }, [closeSidebar]);

  const deleteCurrent = useCallback(() => {
    if (window.confirm("Delete this conversation? This can't be undone.")) removeThread(threadId);
  }, [removeThread, threadId]);

  const showTools = view === "tools";
  const showEmpty = !showTools && messages.length === 0 && !streaming;
  const rail = collapsed && !isMobile;

  const recentCards = useMemo(() => (
    [...threads].reverse()
      .filter(t => t !== threadId && (threadMessages[t]?.length ?? 1) > 0)
      .slice(0, 3)
      .map(tid => ({ tid, title: titles[tid] || "New conversation", count: threadMessages[tid]?.length || 0 }))
  ), [threads, threadId, threadMessages, titles]);

  const lastAiIdx = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) if (messages[i].role === "assistant") return i;
    return -1;
  }, [messages]);

  const composer = (
    <Composer
      hero={showEmpty}
      placeholder={showEmpty ? "Ask SynapseAI anything…" : "Ask a follow-up…"}
      value={input}
      onChange={setInput}
      onSend={send}
      disabled={streaming}
      voiceSupported={voiceSupported}
      voiceMode={voiceMode}
      listening={listening}
      speaking={speaking}
      onVoiceToggle={handleVoiceToggle}
      inputRef={inputElRef}
      attachment={attachment}
      uploading={uploading}
      uploadError={uploadError}
      onAttachClick={() => fileInputRef.current?.click()}
      onRemoveAttachment={removeAttachment}
      fileInputRef={fileInputRef}
      onFileSelect={handleFileSelect}
      onStop={stopGeneration}
      onTools={() => changeView("tools")}
    />
  );

  const voiceBar = (
    <VoiceStatusBar
      voiceMode={voiceMode}
      listening={listening}
      speaking={speaking}
      onStop={handleStopVoice}
      voiceOptions={voiceOptions}
      selectedVoice={selectedVoice}
      onVoiceChange={setSelectedVoice}
    />
  );

  return (
    <div className="app">
      <a className="skip-link" href="#composer-input">Skip to message box</a>
      <TopBar
        view={view}
        health={health}
        isMobile={isMobile}
        collapsed={collapsed}
        onToggleNav={toggleNav}
        onNew={newThread}
        navBtnRef={navBtnRef}
      />

      <div className="workspace">
        <Sidebar
          threads={threads} titles={titles} threadId={threadId}
          onNew={newThread}
          onLoad={tid => { loadThread(tid); closeSidebar(); }}
          onDelete={removeThread}
          onRename={renameThread}
          isOpen={sidebarOpen}
          onClose={closeSidebar}
          view={view}
          onView={changeView}
          historyStatus={historyStatus}
          onRetryHistory={() => setHistoryReload(n => n + 1)}
          rail={rail}
          onExpand={() => { setCollapsed(false); writePref("synapse_sidebar", "open"); }}
        />

        <main className={`main ${showEmpty ? "is-empty" : ""}`}>
          {showTools && <ToolDirectory onTry={fillComposer} />}

          {showEmpty && (
            <EmptyState
              composer={<>
                {voiceBar}
                {voiceError && <div className="inline-alert error composer-alert" role="alert"><AlertIcon size={15} /> {voiceError}</div>}
                {composer}
              </>}
              onPrompt={fillComposer}
              recent={recentCards}
              onOpen={loadThread}
              onTools={() => changeView("tools")}
            />
          )}

          {!showTools && !showEmpty && (
            <>
              <ConversationHeader
                title={titles[threadId] && titles[threadId] !== "New Conversation" ? titles[threadId] : "New conversation"}
                count={messages.length}
                onRename={t => renameThread(threadId, t)}
                onDelete={deleteCurrent}
              />
              <div className="messages" ref={scrollerRef} onScroll={handleMessagesScroll}
                   onWheel={pauseFollowOnWheel} onTouchMove={pauseFollowOnTouch}>
                {messages.map((m, i) => (
                  <Message
                    key={i}
                    index={i}
                    role={m.role}
                    content={m.content}
                    attachmentName={m.attachmentName}
                    restored={m.restored}
                    activity={m.activity}
                    isLast={i === lastAiIdx}
                    reading={readingIdx === i}
                    busy={streaming}
                    canAskAgain={i > 0 && messages[i - 1]?.role === "user" && !messages[i - 1]?.attachmentName}
                    onReadAloud={onReadAloud}
                    onAskAgain={onAskAgain}
                  />
                ))}
                {streaming && (
                  <div className="msg msg-ai">
                    <BrandMark size={30} />
                    <div className="ai-body">
                      {activity && <LiveActivity activity={activity} />}
                      {streamMsg && (
                        <div className="markdown">
                          <ReactMarkdown remarkPlugins={REMARK_PLUGINS} components={STREAMING_MARKDOWN_COMPONENTS}>{streamMsg}</ReactMarkdown>
                          <span className="cursor" aria-hidden="true" />
                        </div>
                      )}
                    </div>
                  </div>
                )}
                {voiceError && (
                  <div className="msg"><div className="inline-alert error" role="alert"><AlertIcon size={15} /> {voiceError}</div></div>
                )}
              </div>
              {showJump && (
                <button type="button" className="jump-btn" onClick={jumpToLatest} aria-label="Jump to latest message" title="Jump to latest">
                  <ArrowDownIcon size={16} />
                </button>
              )}
              {voiceBar}
              {composer}
            </>
          )}
        </main>
      </div>

      <div className="sr-only" aria-live="polite" role="status">{announce}</div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════
// AUTH GATE + ROUTING
// ═══════════════════════════════════════════════════════
function ProtectedRoute({ children }) {
  const { isAuthenticated, loading } = useAuth();
  if (loading) {
    return (
      <div className="auth-loading-screen" role="status">
        <BrandMark size={44} />
        <span className="loading-text"><span className="spinner small" aria-hidden="true" /> Loading your workspace…</span>
      </div>
    );
  }
  if (!isAuthenticated) return <Navigate to="/login" replace />;
  return children;
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/signup" element={<SignupPage />} />
          <Route path="/verify-email" element={<VerifyEmailPage />} />
          <Route path="/forgot-password" element={<ForgotPasswordPage />} />
          <Route path="/reset-password" element={<ResetPasswordPage />} />
          <Route
            path="/*"
            element={
              <ProtectedRoute>
                <ChatApp />
              </ProtectedRoute>
            }
          />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  );
}
