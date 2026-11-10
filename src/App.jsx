import React, { useState, useEffect, useRef, useCallback } from "react";
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
} from "./voiceChat";
import { API_BASE } from "./apiConfig";
import { AuthProvider, useAuth } from "./AuthContext";
import { LoginPage, SignupPage, VerifyEmailPage, ForgotPasswordPage, ResetPasswordPage } from "./AuthPages";
import { BrandMark, Wordmark } from "./Brand";
import ToolDirectory from "./ToolDirectory";
import {
  PlusIcon, MenuIcon, CloseIcon, ChatIcon, ToolsIcon, TrashIcon, PencilIcon, PaperclipIcon,
  MicIcon, SendIcon, StopIcon, LogoutIcon, ChevronUpIcon, FileIcon, ImageIcon,
  AlertIcon, CopyIcon, CheckIcon, SearchIcon, CodeIcon, BookIcon, SparkIcon,
} from "./Icons";

// ═══════════════════════════════════════════════════════
// API
// ═══════════════════════════════════════════════════════
const BASE = API_BASE;

// Labels for the lightweight status events streamed from the backend
// (`data: {"status": ...}`). `using_tools` doesn't say WHICH tool, so the
// label stays generic; `evaluating` / `refining` / `finalizing` come from the
// adaptive agent loop (backend/adaptive_loop.py) — only these status names
// are streamed, never the evaluator's internal content.
const TOOL_STATUS_LABELS = {
  using_tools: "Using tools…",
  evaluating: "Checking the results…",
  refining: "Refining the answer…",
  finalizing: "Finalizing…",
};

const ERROR_PREFIX = "⚠️ **Error:** ";

const api = {
  // credentials: "include" — every call to the protected backend needs to
  // send the httpOnly session cookie set by /auth/login; without it every
  // one of these would come back 401 once a thread/message/etc. is gated.
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

// ═══════════════════════════════════════════════════════
// TOP BAR
// ═══════════════════════════════════════════════════════
function TopBar({ view, health, onMenu, menuRef }) {
  return (
    <header className="topbar">
      <button ref={menuRef} type="button" className="icon-btn menu-btn" onClick={onMenu} aria-label="Open navigation">
        <MenuIcon size={18} />
      </button>
      <div className="topbar-brand">
        <BrandMark size={40} />
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
  view, onView, historyStatus, onRetryHistory,
}) {
  const { user, logout } = useAuth();
  const newBtnRef = useRef(null);

  // Mobile drawer: move focus in on open, close on Escape (spec §10).
  useEffect(() => {
    if (!isOpen) return;
    newBtnRef.current?.focus();
    const onKey = e => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [isOpen, onClose]);

  const recent = [...threads].reverse();

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

        <button ref={newBtnRef} className="btn-primary btn-new" onClick={onNew}>
          <PlusIcon size={16} />
          New conversation
        </button>

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
// EMPTY STATE
// ═══════════════════════════════════════════════════════
// Starters put an editable prompt in the composer — they never send on their
// own (spec §05). The trailing text is left open for the user to finish.
const STARTERS = [
  { icon: SearchIcon, title: "Research a topic",  desc: "Explore a subject in depth",  prompt: "Research the latest developments in " },
  { icon: CodeIcon,   title: "Help me with code", desc: "Write, review or debug",      prompt: "Help me with this code:\n\n" },
  { icon: BookIcon,   title: "Explain a concept", desc: "Understand it clearly",       prompt: "Explain in simple terms how " },
  { icon: SparkIcon,  title: "Help me plan",      desc: "Organise tasks and ideas",    prompt: "Help me plan " },
];

function EmptyState({ onPrompt }) {
  return (
    <div className="hero">
      <div className="hero-inner">
        <BrandMark size={48} />
        <div className="eyebrow">Your AI workspace</div>
        <h1 className="hero-title">What can we explore today?</h1>
        <p className="hero-sub">Ask anything. Connect ideas. Get things done.</p>
        <div className="starter-grid">
          {STARTERS.map(({ icon: Icon, title, desc, prompt }) => (
            <button type="button" className="starter-card" key={title} onClick={() => onPrompt(prompt)}>
              <span className="starter-icon"><Icon size={17} /></span>
              <span className="starter-title">{title}</span>
              <span className="starter-desc">{desc}</span>
            </button>
          ))}
        </div>
      </div>
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
// We take over <pre> to add the language label + copy button.
// While an answer streams, code renders as plain text (no Prism tokenizing):
// re-highlighting the whole growing block every frame blocked the main thread
// and made scrolling lurch. Same box, font and size, so nothing shifts when
// colours appear at the end.
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

// memo: while an answer streams, ChatApp re-renders every frame — without
// this, every earlier message re-parsed its markdown and re-highlighted its
// code each time, so long chats with code got slower and choppier.
const Message = React.memo(function Message({ role, content, streaming, attachmentName, restored }) {
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
    <div className="msg msg-ai">
      <BrandMark size={28} />
      <div className="ai-body">
        {isError
          ? <ErrorMessage content={content} restored={restored} />
          : <div className="markdown">
              <ReactMarkdown remarkPlugins={REMARK_PLUGINS} components={streaming ? STREAMING_MARKDOWN_COMPONENTS : MARKDOWN_COMPONENTS}>
                {content}
              </ReactMarkdown>
              {streaming && <span className="cursor" aria-hidden="true" />}
            </div>}
      </div>
    </div>
  );
});

// Shown while a request is in flight and no answer text has arrived yet.
// The label only changes when the backend streams a status event.
function ToolActivity({ label }) {
  return (
    <div className="msg msg-ai">
      <BrandMark size={28} />
      <div className="ai-body">
        <div className="tool-activity" role="status">
          <span className="spinner small" aria-hidden="true" />
          <span>{label || "Thinking…"}</span>
        </div>
      </div>
    </div>
  );
}

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
  value, onChange, onSend, disabled,
  voiceSupported, voiceMode, listening, speaking,
  onVoiceToggle, inputRef,
  attachment, uploading, uploadError,
  onAttachClick, onRemoveAttachment, fileInputRef, onFileSelect,
  onStop,
}) {
  useEffect(() => {
    const el = inputRef.current;
    if (el) { el.style.height = "auto"; el.style.height = `${el.scrollHeight}px`; }
  }, [value, inputRef]);
  const canSend = (value.trim() || attachment) && !disabled && !uploading;
  const submit = () => { if (canSend) onSend(value, { fromVoice: false }); };
  return (
    <div className="composer-zone">
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

      <div className={`composer ${voiceMode ? "voice-active" : ""} ${disabled ? "is-busy" : ""}`} onClick={e => { if (e.target === e.currentTarget) inputRef.current?.focus(); }}>
        <input
          type="file"
          ref={fileInputRef}
          onChange={onFileSelect}
          accept=".pdf,.docx,image/*"
          hidden
        />
        <button
          type="button"
          className="icon-btn composer-btn"
          onClick={onAttachClick}
          disabled={disabled || uploading}
          title="Attach a PDF, DOCX or image"
          aria-label="Attach a PDF, DOCX or image"
        >
          <PaperclipIcon size={18} />
        </button>
        {voiceSupported && (
          <button
            type="button"
            className={`icon-btn composer-btn mic-btn ${voiceMode ? "on" : ""} ${listening ? "listen" : ""} ${speaking ? "speak" : ""}`}
            onClick={onVoiceToggle}
            disabled={disabled && !voiceMode}
            title={voiceMode ? "Exit voice mode" : "Start voice chat (same tools and memory)"}
            aria-label={voiceMode ? "Exit voice mode" : "Start voice chat"}
            aria-pressed={voiceMode}
          >
            <MicIcon size={18} />
          </button>
        )}
        <label className="sr-only" htmlFor="composer-input">Message SynapseAI</label>
        <textarea
          id="composer-input"
          ref={inputRef}
          rows={1}
          className="input-field"
          placeholder={voiceMode ? "Voice mode — speak or type a message…" : "Message SynapseAI…"}
          value={value}
          disabled={disabled && !voiceMode}
          onChange={e => onChange(e.target.value)}
          onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } }}
          aria-describedby="composer-hint"
        />
        {disabled
          // While an answer is generating (or the model is still thinking /
          // using tools) the send button becomes Stop.
          ? <button type="button" className="send-btn stop-btn" onClick={onStop} title="Stop generating" aria-label="Stop generating">
              <StopIcon size={15} />
            </button>
          : <button type="button" className="send-btn" onClick={submit} disabled={!canSend} aria-label="Send message" title="Send (Enter)">
              <SendIcon size={17} />
            </button>
        }
      </div>
      <p className="composer-hint" id="composer-hint">
        AI-generated responses may be inaccurate. Verify important information.
        <span className="composer-keys"> Enter to send · Shift+Enter for a new line</span>
      </p>
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
  const [toolStatus,  setToolStatus]  = useState(null);
  const [input,       setInput]       = useState("");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [view,        setView]        = useState("assistant"); // "assistant" | "tools"
  const [historyStatus, setHistoryStatus] = useState("loading"); // loading | ready | error
  const [historyReload, setHistoryReload] = useState(0);
  const [health,      setHealth]      = useState("checking");
  const [announce,    setAnnounce]    = useState("");          // screen-reader live region
  const [voiceMode,   setVoiceMode]   = useState(false);
  const [listening,   setListening]   = useState(false);
  const [speaking,    setSpeaking]    = useState(false);
  const [voiceError,  setVoiceError]  = useState(null);
  const [voiceOptions,  setVoiceOptions]  = useState([]);
  const [selectedVoice, setSelectedVoice] = useState(() => localStorage.getItem("synapse_voice") || "browser");

  const [attachment,  setAttachment]  = useState(null);   // { filename, kind, content }
  const [uploading,   setUploading]   = useState(false);
  const [uploadError, setUploadError] = useState(null);
  const fileInputRef = useRef(null);

  const voiceRef = useRef(null);
  const inputElRef = useRef(null);
  const menuBtnRef = useRef(null);
  const sidebarOpenRef = useRef(false);
  sidebarOpenRef.current = sidebarOpen;
  const voiceSupported = isVoiceSupported();

  // threadMessages stores the FULL message history per thread locally in the
  // frontend — independent from backend state. The backend can trim messages
  // for memory management, but the UI always shows the complete conversation.
  const [threadMessages, setThreadMessages] = useState({});

  const streamRef = useRef("");
  const messagesRef = useRef(null);
  // Auto-follow new content only while the user is at the bottom — scrolling
  // up to read mid-stream pauses it; scrolling back down resumes it.
  const followRef = useRef(true);
  const lastAutoTopRef = useRef(null);   // scrollTop we set ourselves
  const prevTopRef = useRef(0);
  const autoUntilRef = useRef(0);        // ignore scroll events during our smooth scroll

  const say = useCallback(msg => {
    // Toggle a zero-width char so repeating the same text is still announced.
    setAnnounce(prev => (prev === msg ? `${msg}​` : msg));
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

  // Auto-scroll. Not scrollIntoView-per-token: during fast streaming (code)
  // each call restarted the previous smooth animation mid-flight, so the view
  // jittered up and down. Instead, one rAF loop eases toward the bottom while
  // streaming, and a single smooth scroll runs when a message is added.
  const handleMessagesScroll = useCallback(() => {
    const el = messagesRef.current;
    if (!el) return;
    const top = el.scrollTop;
    const prevTop = prevTopRef.current;
    prevTopRef.current = top;
    if (Date.now() < autoUntilRef.current) return;
    if (lastAutoTopRef.current !== null && Math.abs(top - lastAutoTopRef.current) < 2) return;
    const distance = el.scrollHeight - el.clientHeight - top;
    // Resume when back at the bottom. "Near + moving down" counts too: during a
    // fast stream new content lands before this event fires, so the user who
    // just reached the bottom is already a few dozen px above it again.
    if (distance < 8 || (top > prevTop && distance < 120)) followRef.current = true;
    else if (top < prevTop - 2 && distance > 80) followRef.current = false;   // moved up, e.g. scrollbar drag
  }, []);

  // Trackpads scroll in tiny steps that never cross the 80px line, so read
  // the intent directly: any upward wheel/touch scroll pauses auto-follow.
  const pauseFollowOnWheel = useCallback(e => { if (e.deltaY < 0) followRef.current = false; }, []);
  const pauseFollowOnTouch = useCallback(() => { followRef.current = false; }, []);

  useEffect(() => {
    const el = messagesRef.current;
    if (!el || !followRef.current) return;
    autoUntilRef.current = Date.now() + 600;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    if (!streaming) return;
    let raf;
    const tick = () => {
      const el = messagesRef.current;
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
    localStorage.setItem("synapse_voice", selectedVoice);
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

  const sendRef = useRef(null);
  const abortRef = useRef(null);          // AbortController of the in-flight answer
  const stopRequestedRef = useRef(false); // distinguishes Stop from a voice interruption

  // Mobile drawer close — returns focus to the menu button that opened it.
  const closeSidebar = useCallback(() => {
    if (!sidebarOpenRef.current) return;
    setSidebarOpen(false);
    requestAnimationFrame(() => menuBtnRef.current?.focus());
  }, []);

  const focusComposer = useCallback((caretToEnd = false) => {
    requestAnimationFrame(() => {
      const el = inputElRef.current;
      if (!el) return;
      el.focus();
      if (caretToEnd) el.setSelectionRange(el.value.length, el.value.length);
    });
  }, []);

  const loadThread = useCallback((tid) => {
    // Never re-fetch from backend when switching threads — use the local
    // threadMessages copy, which has the full history.
    setView("assistant");
    if (tid === threadId) return;
    followRef.current = true;
    setThreadId(tid);
    setMessages(threadMessages[tid] || []);
  }, [threadId, threadMessages]);

  const newThread = useCallback(async () => {
    const tid = await api.newThread();
    setThreadId(tid);
    setMessages([]);
    setView("assistant");
    setThreads(p => [...p, tid]);
    setTitles(p => ({ ...p, [tid]: "New Conversation" }));
    setThreadMessages(p => ({ ...p, [tid]: [] }));
    closeSidebar();
    focusComposer();
  }, [closeSidebar, focusComposer]);

  const removeThread = useCallback(async (tid) => {
    await api.deleteThread(tid);
    setThreads(p => p.filter(t => t !== tid));
    setTitles(p => { const n = { ...p }; delete n[tid]; return n; });
    setThreadMessages(p => { const n = { ...p }; delete n[tid]; return n; });
    if (tid === threadId) { setThreadId(uuid()); setMessages([]); }
  }, [threadId]);

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

    setStreaming(true); streamRef.current = ""; setStreamMsg(""); setToolStatus(null);

    let spokenUpTo = 0;
    const ttsBufferRef = { current: "" };
    // Fast streams (code) deliver many tokens per frame; rendering on every
    // token re-parsed the growing markdown far more often than the screen can
    // show, so the UI fell behind and lurched. Render at most once per frame.
    let renderFrame = null;
    const renderStream = () => { renderFrame = null; setStreamMsg(streamRef.current); };

    try {
      const full = await api.streamChat(
        threadId,
        modelText,
        token => {
          setToolStatus(null);
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
          // Adaptive-loop statuses (evaluating/refining/finalizing) reuse the
          // same activity line as using_tools, while no answer text has streamed yet.
          if (status === "using_tools" || status === "evaluating" || status === "refining" || status === "finalizing") {
            setToolStatus(status);
            if (fromVoice && status === "using_tools") voiceRef.current?.speakStatus("One moment.");
          }
        },
        { signal, voice: fromVoice }
      );

      if (fromVoice && voiceRef.current && full) {
        const tail = ttsBufferRef.current.slice(spokenUpTo).trim();
        if (tail.length >= 8) voiceRef.current.enqueueSpeech(tail);
      }

      // Spec §06: an empty answer is a recoverable error, not a blank bubble.
      if (!full) throw new Error("The assistant didn't return a response.");

      const assistantMsg = { role: "assistant", content: full };
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
          setMessages(p => {
            const updated = [...p, { role: "assistant", content: partial }];
            setThreadMessages(prev => ({ ...prev, [threadId]: updated }));
            return updated;
          });
        }
        if (stopRequestedRef.current) say("Generation stopped.");
        return;
      }
      // Keep the user's input (spec §07): put the text — and attachment —
      // back in the composer so retrying is one click on Send.
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
      setStreaming(false); setStreamMsg(""); setToolStatus(null); streamRef.current = "";
      if (fromVoice) voiceRef.current?.notifyAssistantTurnComplete();
      if (!fromVoice) {
        // Re-focus the input — a disabled→enabled textarea doesn't regain
        // browser focus automatically.
        requestAnimationFrame(() => inputElRef.current?.focus());
      }
    }
  }, [streaming, messages, threadId, threads, attachment, say]);

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

  const handleVoiceToggle = useCallback(() => {
    if (!voiceSupported) {
      setVoiceError("Use Chrome or Edge for voice chat.");
      return;
    }
    const v = voiceRef.current;
    if (!v) return;
    if (v.isVoiceMode()) {
      v.exitVoiceMode();
      setVoiceMode(false);
    } else {
      v.enterVoiceMode();
      setVoiceMode(true);
      setVoiceError(null);
    }
  }, [voiceSupported]);

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

  const showTools = view === "tools";
  const showEmpty = !showTools && messages.length === 0 && !streaming;

  return (
    <div className="app">
      <a className="skip-link" href="#composer-input">Skip to message box</a>
      <TopBar view={view} health={health} onMenu={() => setSidebarOpen(true)} menuRef={menuBtnRef} />

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
        />

        <main className="main">
          {showTools
            ? <ToolDirectory onTry={fillComposer} />
            : showEmpty
              ? <EmptyState onPrompt={fillComposer} />
              : <div className="messages" ref={messagesRef} onScroll={handleMessagesScroll}
                     onWheel={pauseFollowOnWheel} onTouchMove={pauseFollowOnTouch}>
                  {messages.map((m, i) => (
                    <Message key={i} role={m.role} content={m.content} attachmentName={m.attachmentName} restored={m.restored} />
                  ))}
                  {streaming && streamMsg && <Message role="assistant" content={streamMsg} streaming />}
                  {streaming && !streamMsg && <ToolActivity label={TOOL_STATUS_LABELS[toolStatus]} />}
                  {voiceError && (
                    <div className="msg"><div className="inline-alert error" role="alert"><AlertIcon size={15} /> {voiceError}</div></div>
                  )}
                </div>
          }
          {!showTools && (
            <>
              <VoiceStatusBar
                voiceMode={voiceMode}
                listening={listening}
                speaking={speaking}
                onStop={handleStopVoice}
                voiceOptions={voiceOptions}
                selectedVoice={selectedVoice}
                onVoiceChange={setSelectedVoice}
              />
              <Composer
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
              />
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
