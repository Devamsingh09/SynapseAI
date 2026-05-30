import React, { useState, useEffect, useRef, useCallback } from "react";
import ReactMarkdown from "react-markdown";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import { v4 as uuid } from "uuid";
import {
  VoiceRecorder,
  SpeechQueue,
  extractNewSentences,
  transcribeWithFallback,
  checkVoiceStatus,
} from "./voiceChat";

// ═══════════════════════════════════════════════════════
// API
// ═══════════════════════════════════════════════════════
const BASE = process.env.REACT_APP_API_URL || "http://localhost:8000";

const api = {
  newThread:    ()    => fetch(`${BASE}/thread/new`, { method: "POST" }).then(r => r.json()).then(d => d.thread_id),
  getThreads:   ()    => fetch(`${BASE}/threads`).then(r => r.json()).then(d => d.threads || []),

  getHistory:   (tid) => fetch(`${BASE}/thread/${tid}/history`)
    .then(r => r.ok ? r.json() : { messages: [], title: "New Conversation" })
    .then(data => ({
      ...data,
      messages: data.messages || []
    })),

  deleteThread: (tid) => fetch(`${BASE}/thread/${tid}`, { method: "DELETE" }),
  getSummary:   (txt) => fetch(`${BASE}/chat/summary`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: txt })
  }).then(r => r.json()).then(d => d.title || "New Conversation"),

  async streamChat(threadId, message, onToken) {
    const res = await fetch(`${BASE}/chat/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ thread_id: threadId, message }),
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
          if (obj.token) { full += obj.token; onToken(obj.token); }
        } catch (_) {}
      }
    }
    return full;
  }
};

// ═══════════════════════════════════════════════════════
// VOICE HOOK
// ═══════════════════════════════════════════════════════
function useVoice(onResult) {
  const [rec, setRec] = useState(false);
  const supported = "SpeechRecognition" in window || "webkitSpeechRecognition" in window;
  const toggle = useCallback(() => {
    if (!supported) return alert("Speech recognition not supported in this browser.");
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (rec) { setRec(false); return; }
    const r = new SR(); r.lang = "en-US"; r.interimResults = false;
    r.onstart = () => setRec(true); r.onend = () => setRec(false); r.onerror = () => setRec(false);
    r.onresult = (e) => onResult?.(e.results[0][0].transcript);
    r.start();
  }, [rec, supported, onResult]);
  return { rec, supported, toggle };
}

// ═══════════════════════════════════════════════════════
// SIDEBAR
// ═══════════════════════════════════════════════════════
function Sidebar({ threads, titles, threadId, onNew, onLoad, onDelete, onVoice, isOpen, onClose }) {
  const { rec, supported, toggle } = useVoice(onVoice);
  return (
    <>
      <div className={`overlay ${isOpen ? "open" : ""}`} onClick={onClose} />
      <aside className={`sidebar ${isOpen ? "open" : ""}`}>

        {/* Logo */}
        <div className="sidebar-logo">
          <div className="logo-orb">🧠</div>
          <div>
            <div className="logo-name">Synapse AI</div>
            <div className="logo-tag">Powered by LLama 3.3 LLM</div>
          </div>
        </div>

        {/* New chat */}
        <button className="btn-new" onClick={onNew}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
            <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
          </svg>
          New Conversation
        </button>

        {/* Voice */}
        {supported && (
          <button className={`btn-voice ${rec ? "rec" : ""}`} onClick={toggle}>
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
              <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
              <line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/>
            </svg>
            {rec ? "● Stop Recording" : "Voice Input"}
          </button>
        )}

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

function Message({ role, content, streaming }) {
  const isUser = role === "user";
  return (
    <div className={`msg ${isUser ? "msg-user" : "msg-ai"}`}>
      <div className="avatar">{isUser ? "🧑‍💻" : "🧠"}</div>
      <div className="bubble">
        {isUser
          ? <span style={{ whiteSpace: "pre-wrap" }}>{content}</span>
          : <><ReactMarkdown components={{ code: CodeBlock }}>{content}</ReactMarkdown>{streaming && <span className="cursor" />}</>
        }
      </div>
    </div>
  );
}

function Typing() {
  return (
    <div className="msg msg-ai">
      <div className="avatar">🧠</div>
      <div className="bubble"><div className="dots"><span /><span /><span /></div></div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════
// INPUT BAR
// ═══════════════════════════════════════════════════════
function InputBar({ value, onChange, onSend, disabled, voiceMode, onVoiceToggle, voiceReady }) {
  const ref = useRef(null);
  useEffect(() => {
    if (ref.current) { ref.current.style.height = "auto"; ref.current.style.height = `${ref.current.scrollHeight}px`; }
  }, [value]);
  const submit = () => { if (value.trim() && !disabled) onSend(value); };
  return (
    <div className="input-zone">
      <div className="input-shell">
        <textarea ref={ref} rows={1} className="input-field"
          placeholder="Message Synapse AI…  (Enter to send, Shift+Enter for newline)"
          value={value} disabled={disabled || voiceMode}
          onChange={e => onChange(e.target.value)}
          onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } }}
        />
        {voiceReady && (
          <button
            type="button"
            className={`voice-btn ${voiceMode ? "active" : ""}`}
            onClick={onVoiceToggle}
            disabled={disabled && !voiceMode}
            title={voiceMode ? "Exit voice chat" : "Start voice chat"}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
              <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
              <line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/>
            </svg>
          </button>
        )}
        <button className="send-btn" onClick={submit} disabled={disabled || voiceMode || !value.trim()}>
          {disabled
            ? <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" style={{ animation: "spin 0.9s linear infinite" }}><path d="M21 12a9 9 0 1 1-6.219-8.56" /><style>{`@keyframes spin{to{transform:rotate(360deg)}}`}</style></svg>
            : <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><line x1="22" y1="2" x2="11" y2="13" /><polygon points="22 2 15 22 11 13 2 9 22 2" /></svg>
          }
        </button>
      </div>
      <p className="input-hint">
        {voiceMode
          ? "Voice chat active — speak naturally, Synapse will listen and reply aloud."
          : "Synapse AI runs on the cloud — your conversations are safe with us."}
      </p>
    </div>
  );
}

const VOICE_LABELS = {
  idle: "Starting…",
  listening: "Listening…",
  transcribing: "Understanding…",
  thinking: "Thinking…",
  speaking: "Speaking…",
};

function VoicePanel({ phase, onStop }) {
  return (
    <div className="voice-panel">
      <div className={`voice-orb ${phase}`}>
        <span className="voice-orb-core">🎙️</span>
        {(phase === "listening" || phase === "speaking") && (
          <>
            <span className="voice-ring r1" />
            <span className="voice-ring r2" />
            <span className="voice-ring r3" />
          </>
        )}
      </div>
      <div className="voice-status">{VOICE_LABELS[phase] || phase}</div>
      <button type="button" className="voice-stop" onClick={onStop}>Stop Voice Chat</button>
    </div>
  );
}

// ═══════════════════════════════════════════════════════
// ROOT APP
// ═══════════════════════════════════════════════════════
export default function App() {
  const [threadId,    setThreadId]    = useState(uuid);
  const [messages,    setMessages]    = useState([]);
  const [threads,     setThreads]     = useState([]);
  const [titles,      setTitles]      = useState({});
  const [streaming,   setStreaming]   = useState(false);
  const [streamMsg,   setStreamMsg]   = useState("");
  const [input,       setInput]       = useState("");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [voiceMode,    setVoiceMode]    = useState(false);
  const [voicePhase,   setVoicePhase]   = useState("idle");
  const [voiceReady,   setVoiceReady]   = useState(false);

  // ✅ FIX: threadMessages stores the FULL message history per thread
  // locally in the frontend — completely independent from backend state.
  // Backend can delete messages for memory management, but the UI
  // always shows the complete conversation the user has seen.
  const [threadMessages, setThreadMessages] = useState({});

  const streamRef = useRef("");
  const bottomRef = useRef(null);
  const speechQueueRef = useRef(new SpeechQueue());
  const recorderRef = useRef(null);
  const voiceModeRef = useRef(false);
  const streamingRef = useRef(false);
  const voiceLoopRef = useRef(false);
  const sendMessageRef = useRef(null);
  const startListeningRef = useRef(null);

  useEffect(() => { voiceModeRef.current = voiceMode; }, [voiceMode]);
  useEffect(() => { streamingRef.current = streaming; }, [streaming]);

  useEffect(() => {
    checkVoiceStatus().then(s => setVoiceReady(s.stt || s.tts));
  }, []);

  const stopVoiceMode = useCallback(() => {
    voiceLoopRef.current = false;
    recorderRef.current?.stop();
    recorderRef.current = null;
    speechQueueRef.current.stop();
    setVoiceMode(false);
    setVoicePhase("idle");
  }, []);

  const startListening = useCallback(() => {
    if (!voiceModeRef.current || streamingRef.current || voiceLoopRef.current) return;

    voiceLoopRef.current = true;
    setVoicePhase("listening");

    const recorder = new VoiceRecorder({
      onSilence: async (blob) => {
        voiceLoopRef.current = false;
        if (!voiceModeRef.current || !blob?.size) {
          if (voiceModeRef.current) startListeningRef.current?.();
          return;
        }
        try {
          setVoicePhase("transcribing");
          const text = await transcribeWithFallback(blob);
          if (!text || !voiceModeRef.current) {
            if (voiceModeRef.current) startListeningRef.current?.();
            return;
          }
          await sendMessageRef.current?.(text, { fromVoice: true });
        } catch {
          if (voiceModeRef.current) {
            setVoicePhase("listening");
            startListeningRef.current?.();
          }
        }
      },
      onMaxDuration: async (blob) => {
        voiceLoopRef.current = false;
        if (!voiceModeRef.current || !blob?.size) return;
        try {
          setVoicePhase("transcribing");
          const text = await transcribeWithFallback(blob);
          if (text && voiceModeRef.current) await sendMessageRef.current?.(text, { fromVoice: true });
          else if (voiceModeRef.current) startListeningRef.current?.();
        } catch {
          if (voiceModeRef.current) startListeningRef.current?.();
        }
      },
      onError: () => {
        voiceLoopRef.current = false;
        if (voiceModeRef.current) {
          alert("Microphone access is required for voice chat.");
          stopVoiceMode();
        }
      },
    });

    recorderRef.current = recorder;
    recorder.start().catch(() => {
      voiceLoopRef.current = false;
      stopVoiceMode();
    });
  }, [stopVoiceMode]);

  startListeningRef.current = startListening;

  const toggleVoiceMode = useCallback(() => {
    if (voiceMode) {
      stopVoiceMode();
      return;
    }
    speechQueueRef.current.reset();
    setVoiceMode(true);
    setVoicePhase("idle");
    setTimeout(() => startListening(), 100);
  }, [voiceMode, stopVoiceMode, startListening]);

  // Load threads on mount
  useEffect(() => {
    api.getThreads().then(ids => {
      setThreads(ids);
      ids.forEach(tid =>
        api.getHistory(tid).then(h => {
          setTitles(p => ({ ...p, [tid]: h.title || "New Conversation" }));
          // ✅ Only populate threadMessages if we don't already have it locally
          setThreadMessages(p => {
            if (p[tid]) return p; // already have local copy, don't overwrite
            return { ...p, [tid]: h.messages || [] };
          });
        })
      );
    });
  }, []);

  // Auto-scroll
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, streamMsg]);

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

  const sendMessage = useCallback(async (text, { fromVoice = false } = {}) => {
    if (!text.trim() || streaming) return;
    if (!fromVoice) setInput("");

    if (!threads.includes(threadId)) setThreads(p => [...p, threadId]);

    const userMsg = { role: "user", content: text };
    setMessages(p => {
      const updated = [...p, userMsg];
      setThreadMessages(prev => ({ ...prev, [threadId]: updated }));
      return updated;
    });

    const isFirst = messages.filter(m => m.role === "user").length === 0;
    if (isFirst) {
      text.length >= 15
        ? api.getSummary(text).then(t => setTitles(p => ({ ...p, [threadId]: t })))
        : setTitles(p => ({ ...p, [threadId]: "New Conversation" }));
    }

    setStreaming(true);
    if (fromVoice) setVoicePhase("thinking");
    streamRef.current = "";
    setStreamMsg("");

    let spokenUpTo = 0;
    const speechQ = speechQueueRef.current;
    if (fromVoice) speechQ.reset();

    try {
      const full = await api.streamChat(threadId, text, token => {
        streamRef.current += token;
        setStreamMsg(streamRef.current);

        if (fromVoice) {
          const { sentences, newSpokenUpTo } = extractNewSentences(streamRef.current, spokenUpTo);
          spokenUpTo = newSpokenUpTo;
          sentences.forEach(s => {
            setVoicePhase("speaking");
            speechQ.enqueue(s);
          });
        }
      });

      if (full) {
        const assistantMsg = { role: "assistant", content: full };
        setMessages(p => {
          const updated = [...p, assistantMsg];
          setThreadMessages(prev => ({ ...prev, [threadId]: updated }));
          return updated;
        });
        if (fromVoice) {
          const remainder = full.slice(spokenUpTo).trim();
          setVoicePhase("speaking");
          await speechQ.flushRemainder(remainder);
          if (voiceModeRef.current) {
            setVoicePhase("listening");
            startListeningRef.current?.();
          }
        }
      } else if (fromVoice && voiceModeRef.current) {
        startListeningRef.current?.();
      }
    } catch (err) {
      const errMsg = { role: "assistant", content: `⚠️ **Error:** ${err.message}` };
      setMessages(p => {
        const updated = [...p, errMsg];
        setThreadMessages(prev => ({ ...prev, [threadId]: updated }));
        return updated;
      });
      if (fromVoice) {
        setVoicePhase("speaking");
        await speechQ.enqueue(`Error: ${err.message}`);
        if (voiceModeRef.current) {
          setVoicePhase("listening");
          startListeningRef.current?.();
        }
      }
    } finally {
      setStreaming(false);
      setStreamMsg("");
      streamRef.current = "";
    }
  }, [streaming, messages, threadId, threads]);

  sendMessageRef.current = sendMessage;

  const send = useCallback((text) => sendMessage(text, { fromVoice: false }), [sendMessage]);

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
        onVoice={t => setInput(t)}
        isOpen={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
      />

      <main className="main">
        {showHero
          ? <Hero onPrompt={send} />
          : <div className="messages">
              {messages.map((m, i) => <Message key={i} role={m.role} content={m.content} />)}
              {streaming && streamMsg  && <Message role="assistant" content={streamMsg} streaming />}
              {streaming && !streamMsg && <Typing />}
              <div ref={bottomRef} />
            </div>
        }
        <InputBar
          value={input}
          onChange={setInput}
          onSend={send}
          disabled={streaming}
          voiceMode={voiceMode}
          onVoiceToggle={toggleVoiceMode}
          voiceReady={voiceReady}
        />
        {voiceMode && <VoicePanel phase={voicePhase} onStop={stopVoiceMode} />}
      </main>
    </div>
  );
}
