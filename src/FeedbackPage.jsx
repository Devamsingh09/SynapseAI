import React, { useEffect, useRef, useState } from "react";
import { API_BASE } from "./apiConfig";
import { useAuth } from "./AuthContext";
import { AlertIcon, CheckIcon, GitIcon, MailIcon, SendIcon } from "./Icons";

// The person feedback goes to. The backend sends the mail (POST /feedback,
// delivered to FEEDBACK_TO_EMAIL); this card is what users see.
export const CREATOR = {
  name: "Devam Singh",
  initials: "DS",
  role: "Creator of SynapseAI",
  email: "devamsingh0009@gmail.com",
  photo: "/devam.jpg",
  github: "https://github.com/Devamsingh09",
  githubLabel: "github.com/Devamsingh09",
};

const MAX_LEN = 1000;
const MIN_LEN = 3;
const NAME_KEY = "synapse_feedback_name";

function readName() {
  try { return localStorage.getItem(NAME_KEY) || ""; } catch { return ""; }
}
function writeName(name) {
  try { localStorage.setItem(NAME_KEY, name); } catch { /* storage blocked: fine */ }
}

async function postFeedback(name, message) {
  let res;
  try {
    res = await fetch(`${API_BASE}/feedback`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, message }),
    });
  } catch {
    throw new Error("We couldn't reach the server. Check your connection and try again.");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (res.status === 401) throw new Error("Your session has expired. Sign in again to send feedback.");
    // FastAPI validation errors arrive as a list of { msg }.
    const msg = Array.isArray(data.detail) ? data.detail[0]?.msg : data.detail;
    throw new Error((msg || `Couldn't send your feedback (${res.status}).`).replace(/^Value error, /, ""));
  }
  return data;
}

function CreatorCard() {
  const [photoOk, setPhotoOk] = useState(true);
  return (
    <section className="creator-card" aria-label={`About ${CREATOR.name}`}>
      <div className="creator-photo">
        {photoOk
          ? <img src={CREATOR.photo} alt={`${CREATOR.name}, ${CREATOR.role.toLowerCase()}`} width="112" height="112" onError={() => setPhotoOk(false)} />
          : <span className="creator-initials" aria-hidden="true">{CREATOR.initials}</span>}
      </div>
      <h2 className="creator-name">{CREATOR.name}</h2>
      <div className="creator-role">{CREATOR.role}</div>
      <p className="creator-note">
        Every message lands in my inbox with your name and email, so I can reply to you directly.
      </p>
      <div className="creator-links">
        <a className="creator-link" href={`mailto:${CREATOR.email}`}>
          <MailIcon size={15} /> <span>{CREATOR.email}</span>
        </a>
        <a className="creator-link" href={CREATOR.github} target="_blank" rel="noopener noreferrer">
          <GitIcon size={15} /> <span>{CREATOR.githubLabel}</span>
        </a>
      </div>
    </section>
  );
}

export default function FeedbackPage({ onBack }) {
  const { user } = useAuth();
  const [name, setName] = useState(readName);
  const [message, setMessage] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState(null);
  const [sentAs, setSentAs] = useState(null);
  const nameRef = useRef(null);
  const messageRef = useRef(null);
  const sentHeadingRef = useRef(null);

  useEffect(() => {
    if (sentAs) sentHeadingRef.current?.focus();
  }, [sentAs]);

  const submit = async (e) => {
    e?.preventDefault();
    if (sending) return;
    const cleanName = name.replace(/\s+/g, " ").trim();
    const cleanMessage = message.trim();
    if (!cleanName) { setError("Please enter your name."); nameRef.current?.focus(); return; }
    if (cleanMessage.length < MIN_LEN) {
      setError("Please write a little feedback before sending.");
      messageRef.current?.focus();
      return;
    }
    setSending(true);
    setError(null);
    try {
      await postFeedback(cleanName, cleanMessage);
      writeName(cleanName);
      setMessage("");
      setSentAs(cleanName);
    } catch (err) {
      setError(err.message);   // the message stays in the box so nothing is lost
    } finally {
      setSending(false);
    }
  };

  const sendAnother = () => {
    setSentAs(null);
    requestAnimationFrame(() => messageRef.current?.focus());
  };

  return (
    <div className="tools-view feedback-view">
      <div className="tools-inner feedback-inner">
        <header className="tools-head">
          <div className="eyebrow">Feedback</div>
          <h1 className="page-title">Help shape SynapseAI</h1>
          <p className="page-sub">
            Found a bug, missing a feature, or enjoying something? Send a quick note and it goes straight to my inbox.
          </p>
        </header>

        <div className="feedback-grid">
          <CreatorCard />

          <section className="feedback-card" aria-label="Send feedback">
            {sentAs ? (
              <div className="feedback-sent" role="status">
                <span className="sent-badge" aria-hidden="true"><CheckIcon size={26} /></span>
                <h2 className="sent-title" tabIndex={-1} ref={sentHeadingRef}>Thanks, {sentAs}!</h2>
                <p className="sent-text">
                  Your feedback is on its way to {CREATOR.name.split(" ")[0]}. Any reply will come to <strong>{user?.email}</strong>.
                </p>
                <div className="sent-actions">
                  <button type="button" className="btn-primary" onClick={sendAnother}>Send more feedback</button>
                  <button type="button" className="btn-ghost" onClick={onBack}>Back to chat</button>
                </div>
              </div>
            ) : (
              <form className="feedback-form" onSubmit={submit} noValidate>
                <h2 className="feedback-card-title">Send feedback</h2>

                <div className="field">
                  <label className="field-label" htmlFor="fb-name">Your name</label>
                  <input
                    id="fb-name"
                    ref={nameRef}
                    className="field-input"
                    value={name}
                    onChange={e => setName(e.target.value)}
                    maxLength={80}
                    autoComplete="name"
                    placeholder="e.g. Asha Rao"
                    required
                  />
                </div>

                <div className="sending-as">
                  <MailIcon size={14} />
                  <span>Sending as <strong>{user?.email}</strong></span>
                </div>

                <div className="field">
                  <div className="field-row">
                    <label className="field-label" htmlFor="fb-message">Your feedback</label>
                    <span className={`fb-count ${message.length > MAX_LEN - 100 ? "near" : ""}`} aria-hidden="true">
                      {message.length}/{MAX_LEN}
                    </span>
                  </div>
                  <textarea
                    id="fb-message"
                    ref={messageRef}
                    className="field-input feedback-text"
                    value={message}
                    onChange={e => setMessage(e.target.value)}
                    onKeyDown={e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) submit(e); }}
                    rows={4}
                    maxLength={MAX_LEN}
                    placeholder="What's working, what isn't, what you'd love to see…"
                    aria-describedby="fb-hint"
                    required
                  />
                </div>

                {error && <div className="inline-alert error" role="alert"><AlertIcon size={15} /> {error}</div>}

                <div className="feedback-foot">
                  <span id="fb-hint" className="fb-hint">
                    {CREATOR.name.split(" ")[0]} receives your name, email and message.<span className="fb-keys"> <kbd className="kbd">Ctrl Enter</kbd> to send.</span>
                  </span>
                  <button className="btn-primary" type="submit" disabled={sending} aria-busy={sending}>
                    {sending ? <><span className="spinner" aria-hidden="true" /> Sending…</> : <><SendIcon size={15} /> Send feedback</>}
                  </button>
                </div>
              </form>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}
