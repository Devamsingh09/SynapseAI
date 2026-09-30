import React, { useState, useEffect, useRef, useCallback } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeRaw from "rehype-raw";
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

// ═══════════════════════════════════════════════════════
// API
// ═══════════════════════════════════════════════════════
const BASE = API_BASE;

// Labels for lightweight "typing" status indicators streamed from the
// backend. `using_tools` already existed; `evaluating` / `refining` /
// `finalizing` come from the adaptive agent loop (see backend/adaptive_loop.py)
// — internal evaluator content itself is never streamed, only these status names.
const TOOL_STATUS_LABELS = {
  using_tools: "Searching the web & using tools…",
  evaluating: "Checking the results…",
  refining: "Refining the answer…",
  finalizing: "Finalizing…",
};

const api = {
  // credentials: "include" — every call to the protected backend needs to
  // send the httpOnly session cookie set by /auth/login; without it every
  // one of these would come back 401 once a thread/message/etc. is gated.
  newThread:    ()    => fetch(`${BASE}/thread/new`, { method: "POST", credentials: "include" }).then(r => r.json()).then(d => d.thread_id),
  getThreads:   ()    => fetch(`${BASE}/threads`, { credentials: "include" }).then(r => r.json()).then(d => d.threads || []),

  getHistory:   (tid) => fetch(`${BASE}/thread/${tid}/history`, { credentials: "include" })
    .then(r => r.ok ? r.json() : { messages: [], title: "New Conversation" })
    .then(data => ({
      ...data,
      messages: data.messages || []
    })),

  deleteThread: (tid) => fetch(`${BASE}/thread/${tid}`, { method: "DELETE", credentials: "include" }),
  getSummary:   (txt) => fetch(`${BASE}/chat/summary`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: txt })
  }).then(r => r.json()).then(d => d.title || "New Conversation"),

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

// ═══════════════════════════════════════════════════════
// SIDEBAR
// ═══════════════════════════════════════════════════════
function Sidebar({ threads, titles, threadId, onNew, onLoad, onDelete, isOpen, onClose }) {
  const { user, logout } = useAuth();

  return (
    <>
      <div className={`overlay ${isOpen ? "open" : ""}`} onClick={onClose} />
      <aside className={`sidebar ${isOpen ? "open" : ""}`}>

        {/* Logo */}
        <div className="sidebar-logo">
          <div className="logo-orb">🧠</div>
          <div>
            <div className="logo-name">Synapse AI</div>
            <div className="logo-tag">Powered by GPT-OSS 120B</div>
          </div>
        </div>

        {/* New chat */}
        <button className="btn-new" onClick={onNew}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
            <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
          </svg>
          New Conversation
        </button>

        {/* History */}
        <nav className="history">
          <span className="h-label">Recent Chats</span>
          {[...threads].reverse().map(tid => (
            <div className="t-row" key={tid}>
              <button
                className={`t-btn ${tid === threadId ? "active" : ""}`}
                onClick={() => { onLoad(tid); onClose?.(); }}
                title={titles[tid] || tid}
              >
                {titles[tid] || "New Conversation"}
              </button>
              <button className="t-del" onClick={e => { e.stopPropagation(); onDelete(tid); }}>✕</button>
            </div>
          ))}
          {threads.length === 0 && (
            <p style={{ fontSize: 12, color: "var(--text-dim)", padding: "10px 8px" }}>No conversations yet</p>
          )}
        </nav>

        {user && (
          <div className="topbar-user">
            <span className="topbar-user-email" title={user.email}>{user.email}</span>
            <button className="topbar-logout-btn" onClick={logout}>Log out</button>
          </div>
        )}
      </aside>
    </>
  );
}

// ═══════════════════════════════════════════════════════
// HERO
// ═══════════════════════════════════════════════════════
const CARDS = [
  { icon: "🐍", title: "Write Python Code",   desc: "Scripts, automation, data pipelines — generated instantly.", prompt: "Write a Python script to automate a daily task." },
  { icon: "📊", title: "Analyze Data",         desc: "Pandas, visualizations, stats — explained step by step.",    prompt: "How do I use Pandas to analyze a CSV file?" },
  { icon: "💡", title: "Explain Any Concept", desc: "Complex ideas broken down into clear, simple language.",     prompt: "Explain how neural networks work in simple terms." },
];

function Hero({ onPrompt }) {
  return (
    <div className="hero">
      <div className="hero-head">
        <div className="hero-eyebrow">Multi-Purpose AI Assistant</div>
        <h1 className="hero-title">What can I help you<br /><span>build today?</span></h1>
        <p className="hero-sub">Ask anything — code, data, concepts, or just a conversation.</p>
      </div>
      <div className="hero-cards">
        {CARDS.map(c => (
          <div className="hero-card" key={c.title} onClick={() => onPrompt(c.prompt)}>
            <span className="c-icon">{c.icon}</span>
            <span className="c-title">{c.title}</span>
            <span className="c-desc">{c.desc}</span>
            <span className="c-arrow">→</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════
// MESSAGE
// ═══════════════════════════════════════════════════════
function CodeBlock({ inline, className, children, ...props }) {
  const match = /language-(\w+)/.exec(className || "");
  return !inline && match
    ? <SyntaxHighlighter style={oneDark} language={match[1]} PreTag="div"
        customStyle={{ background: "#04070d", border: "1px solid #1c2a3a", borderRadius: "10px", fontSize: "13px", margin: "10px 0" }} {...props}>
        {String(children).replace(/\n$/, "")}
      </SyntaxHighlighter>
    : <code className={className} {...props}>{children}</code>;
}

function Message({ role, content, streaming, attachmentName }) {
  const isUser = role === "user";
  return (
    <div className={`msg ${isUser ? "msg-user" : "msg-ai"}`}>
      <div className="avatar">{isUser ? "🧑‍💻" : "🧠"}</div>
      <div className="bubble">
        {attachmentName && <div className="msg-attachment">📎 {attachmentName}</div>}
        {isUser
          ? <span style={{ whiteSpace: "pre-wrap" }}>{content}</span>
          : <><ReactMarkdown components={{ code: CodeBlock }}>{content}</ReactMarkdown>{streaming && <span className="cursor" />}</>
        }
      </div>
    </div>
  );
}

function Typing({ label }) {
  return (
    <div className="msg msg-ai">
      <div className="avatar">🧠</div>
      <div className="bubble">
        {label && <div className="tool-status-label">{label}</div>}
        <div className="dots"><span /><span /><span /></div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════
// INPUT BAR
// ═══════════════════════════════════════════════════════
function VoiceStatusBar({ voiceMode, listening, speaking, onStop, voiceOptions, selectedVoice, onVoiceChange }) {
  if (!voiceMode) return null;
  let label = "Voice mode — speak when ready";
  if (listening) label = "Listening…";
  else if (speaking) label = "Speaking… tap mic to interrupt";
  else label = "Stand by — listening again in ~1.2s after reply";
  return (
    <div className="voice-status-bar">
      <span className={`voice-pulse ${listening ? "listen" : speaking ? "speak" : ""}`} />
      <span className="voice-status-text">{label}</span>
      <select
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
      <button type="button" className="voice-stop-btn" onClick={onStop}>Stop voice</button>
    </div>
  );
}

function InputBar({
  value, onChange, onSend, disabled,
  voiceSupported, voiceMode, listening, speaking,
  onVoiceToggle, inputRef,
  attachment, uploading, uploadError,
  onAttachClick, onRemoveAttachment, fileInputRef, onFileSelect,
}) {
  const ref = inputRef || useRef(null);
  useEffect(() => {
    if (ref.current) { ref.current.style.height = "auto"; ref.current.style.height = `${ref.current.scrollHeight}px`; }
  }, [value]);
  const canSend = (value.trim() || attachment) && !disabled && !uploading;
  const submit = () => { if (canSend) onSend(value, { fromVoice: false }); };
  return (
    <div className="input-zone">
      {attachment && (
        <div className="attachment-chip">
          <span>{attachment.kind === "image" ? "🖼️" : "📄"}</span>
          <span className="attachment-name">{attachment.filename}</span>
          <button type="button" className="attachment-remove" onClick={onRemoveAttachment} aria-label="Remove attachment">✕</button>
        </div>
      )}
      {uploading && <div className="attachment-chip attachment-loading">Reading file…</div>}
      {uploadError && <div className="file-error-banner">{uploadError}</div>}
      <div className={`input-shell ${voiceMode ? "voice-active" : ""}`}>
        <input
          type="file"
          ref={fileInputRef}
          onChange={onFileSelect}
          accept=".pdf,.docx,image/*"
          style={{ display: "none" }}
        />
        <button
          type="button"
          className="attach-btn"
          onClick={onAttachClick}
          disabled={disabled || uploading}
          title="Attach a PDF, DOCX, or image"
          aria-label="Attach file"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
          </svg>
        </button>
        {voiceSupported && (
          <button
            type="button"
            className={`mic-btn ${voiceMode ? "on" : ""} ${listening ? "listen" : ""} ${speaking ? "speak" : ""}`}
            onClick={onVoiceToggle}
            disabled={disabled && !voiceMode}
            title={voiceMode ? "Exit voice mode" : "Start voice chat (same tools & memory)"}
            aria-label={voiceMode ? "Exit voice mode" : "Start voice chat"}
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
              <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
              <line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/>
            </svg>
          </button>
        )}
        <textarea ref={ref} rows={1} className="input-field"
          placeholder={voiceMode ? "Voice mode — speak or type a message…" : "Message Synapse AI…  (Enter to send, Shift+Enter for newline)"}
          value={value} disabled={disabled && !voiceMode}
          onChange={e => onChange(e.target.value)}
          onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } }}
        />
        <button className="send-btn" onClick={submit} disabled={!canSend}>
          {disabled
            ? <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" style={{ animation: "spin 0.9s linear infinite" }}><path d="M21 12a9 9 0 1 1-6.219-8.56" /><style>{`@keyframes spin{to{transform:rotate(360deg)}}`}</style></svg>
            : <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><line x1="22" y1="2" x2="11" y2="13" /><polygon points="22 2 15 22 11 13 2 9 22 2" /></svg>
          }
        </button>
      </div>
      <p className="input-hint">Synapse AI runs on the cloud — your conversations are safe with us.</p>
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
  const [streaming,   setStreaming]   = useState(false);
  const [streamMsg,   setStreamMsg]   = useState("");
  const [toolStatus,  setToolStatus]  = useState(null);
  const [input,       setInput]       = useState("");
  const [sidebarOpen, setSidebarOpen] = useState(false);
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
  const voiceSupported = isVoiceSupported();

  // ✅ FIX: threadMessages stores the FULL message history per thread
  // locally in the frontend — completely independent from backend state.
  // Backend can delete messages for memory management, but the UI
  // always shows the complete conversation the user has seen.
  const [threadMessages, setThreadMessages] = useState({});

  const streamRef = useRef("");
  const bottomRef = useRef(null);

  // Load threads on mount (retry while backend warms up — embeddings can take 15–30s)
  useEffect(() => {
    let cancelled = false;
    let retryTimer = null;

    const loadThreads = (attempt = 0) => {
      api.getThreads()
        .then(ids => {
          if (cancelled) return;
          setThreads(ids);
          ids.forEach(tid =>
            api.getHistory(tid).then(h => {
              if (cancelled) return;
              setTitles(p => ({ ...p, [tid]: h.title || "New Conversation" }));
              setThreadMessages(p => {
                if (p[tid]) return p;
                return { ...p, [tid]: h.messages || [] };
              });
            }).catch(() => { /* history optional on startup */ })
          );
        })
        .catch(() => {
          if (cancelled || attempt >= 12) return;
          retryTimer = setTimeout(() => loadThreads(attempt + 1), 2000);
        });
    };

    loadThreads();
    return () => {
      cancelled = true;
      if (retryTimer) clearTimeout(retryTimer);
    };
  }, []);

  // Auto-scroll
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, streamMsg]);

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

  const loadThread = useCallback((tid) => {
    // ✅ FIX: Never re-fetch from backend when switching threads.
    // Always use our local threadMessages copy which has the full history.
    if (tid === threadId) return;
    setThreadId(tid);
    setMessages(threadMessages[tid] || []);
  }, [threadId, threadMessages]);

  const newThread = useCallback(async () => {
    const tid = await api.newThread();
    setThreadId(tid);
    setMessages([]);
    setThreads(p => [...p, tid]);
    setTitles(p => ({ ...p, [tid]: "New Conversation" }));
    setThreadMessages(p => ({ ...p, [tid]: [] }));
    setSidebarOpen(false);
  }, []);

  const removeThread = useCallback(async (tid) => {
    await api.deleteThread(tid);
    setThreads(p => p.filter(t => t !== tid));
    setTitles(p => { const n = { ...p }; delete n[tid]; return n; });
    // ✅ Also clean up local message store
    setThreadMessages(p => { const n = { ...p }; delete n[tid]; return n; });
    if (tid === threadId) { setThreadId(uuid()); setMessages([]); }
  }, [threadId]);

  const send = useCallback(async (text, options = {}) => {
    const { fromVoice = false } = options;
    const currentAttachment = fromVoice ? null : attachment; // voice mode stays text-only
    if ((!text.trim() && !currentAttachment) || streaming) return;

    voiceRef.current?.interrupt();
    const signal = fromVoice ? voiceRef.current?.newAbortSignal() : undefined;

    setInput("");
    setVoiceError(null);
    if (currentAttachment) setAttachment(null);

    if (!threads.includes(threadId)) setThreads(p => [...p, threadId]);

    // ✅ FIX: Append user message both to display state AND local thread store
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

    // Auto-title on first message
    const isFirst = messages.filter(m => m.role === "user").length === 0;
    if (isFirst) {
      displayText.length >= 15
        ? api.getSummary(displayText).then(t => setTitles(p => ({ ...p, [threadId]: t })))
        : setTitles(p => ({ ...p, [threadId]: "New Conversation" }));
    }

    setStreaming(true); streamRef.current = ""; setStreamMsg(""); setToolStatus(null);

    let spokenUpTo = 0;
    const ttsBufferRef = { current: "" };

    try {
      const full = await api.streamChat(
        threadId,
        modelText,
        token => {
          setToolStatus(null);
          streamRef.current += token;
          setStreamMsg(streamRef.current);

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
          // same lightweight "typing label" UI as using_tools — no new panel,
          // just a different label while no answer text has streamed yet.
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

      if (full) {
        // ✅ FIX: Append assistant message both to display state AND local thread store
        const assistantMsg = { role: "assistant", content: full };
        setMessages(p => {
          const updated = [...p, assistantMsg];
          setThreadMessages(prev => ({ ...prev, [threadId]: updated }));
          return updated;
        });
      }
    } catch (err) {
      if (err.name === "AbortError") return;
      const errMsg = { role: "assistant", content: `⚠️ **Error:** ${err.message}` };
      setMessages(p => {
        const updated = [...p, errMsg];
        setThreadMessages(prev => ({ ...prev, [threadId]: updated }));
        return updated;
      });
      if (fromVoice) voiceRef.current?.speakStatus("Sorry, something went wrong.");
    } finally {
      setStreaming(false); setStreamMsg(""); setToolStatus(null); streamRef.current = "";
      if (fromVoice) voiceRef.current?.notifyAssistantTurnComplete();
      if (!fromVoice) {
        // Re-focus the input — a disabled→enabled textarea doesn't regain
        // browser focus automatically, so without this you have to click
        // back into the box before typing again after every AI reply.
        requestAnimationFrame(() => inputElRef.current?.focus());
      }
    }
  }, [streaming, messages, threadId, threads, attachment]);

  sendRef.current = send;

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
      setAttachment({ filename: file.name, kind: result.kind, content: result.content });
    } catch (err) {
      setUploadError(err.message || "Upload failed");
    } finally {
      setUploading(false);
    }
  }, []);

  const removeAttachment = useCallback(() => setAttachment(null), []);

  const showHero = messages.length === 0 && !streaming;

  return (
    <div className="app">
      {/* Mobile hamburger */}
      <button className="hamburger" onClick={() => setSidebarOpen(true)}>
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <line x1="3" y1="6" x2="21" y2="6" /><line x1="3" y1="12" x2="21" y2="12" /><line x1="3" y1="18" x2="21" y2="18" />
        </svg>
      </button>

      <Sidebar
        threads={threads} titles={titles} threadId={threadId}
        onNew={newThread}
        onLoad={tid => { loadThread(tid); setSidebarOpen(false); }}
        onDelete={removeThread}
        isOpen={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
      />

      <main className="main">
        {showHero
          ? <Hero onPrompt={send} />
          : <div className="messages">
              {messages.map((m, i) => <Message key={i} role={m.role} content={m.content} attachmentName={m.attachmentName} />)}
              {streaming && streamMsg  && <Message role="assistant" content={streamMsg} streaming />}
              {streaming && !streamMsg && TOOL_STATUS_LABELS[toolStatus] && (
                <Typing label={TOOL_STATUS_LABELS[toolStatus]} />
              )}
              {streaming && !streamMsg && !TOOL_STATUS_LABELS[toolStatus] && <Typing />}
              {voiceError && (
                <div className="voice-error-banner">{voiceError}</div>
              )}
              <div ref={bottomRef} />
            </div>
        }
        <VoiceStatusBar
          voiceMode={voiceMode}
          listening={listening}
          speaking={speaking}
          onStop={handleStopVoice}
          voiceOptions={voiceOptions}
          selectedVoice={selectedVoice}
          onVoiceChange={setSelectedVoice}
        />
        <InputBar
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
        />
      </main>
    </div>
  );
}

// ═══════════════════════════════════════════════════════
// AUTH GATE + ROUTING
// ═══════════════════════════════════════════════════════
function ProtectedRoute({ children }) {
  const { isAuthenticated, loading } = useAuth();
  if (loading) return <div className="auth-loading-screen">Loading…</div>;
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