/**
 * Voice layer for Synapse AI — same chat pipeline as text (POST /chat/stream).
 * STT: Web Speech API (browser, free, low latency)
 * TTS: backend Edge TTS (/voice/tts) — neural voices, interruptible queue
 */

import { API_BASE } from "./apiConfig";

const BASE = API_BASE;

const SENTENCE_RE = /[^.!?]+[.!?]+(?:\s|$)/g;
const MIN_CHUNK_LEN = 6;
const CLAUSE_BREAK_LEN = 40;
/** Wait this long after the assistant finishes speaking before listening again */
const LISTEN_RESUME_DELAY_MS = 1200;

export function isVoiceSupported() {
  return !!(window.SpeechRecognition || window.webkitSpeechRecognition);
}

/**
 * Pull speakable chunks from streamed text — full sentences first, then clause breaks
 * so TTS starts sooner instead of waiting for a long paragraph.
 */
export function extractNewSpeechChunks(buffer, spokenUpTo) {
  const slice = buffer.slice(spokenUpTo);
  const chunks = [];
  let offset = 0;

  SENTENCE_RE.lastIndex = 0;
  let match;
  while ((match = SENTENCE_RE.exec(slice)) !== null) {
    const raw = match[0];
    const s = raw.trim();
    if (!/[.!?]$/.test(s)) break;
    if (s.length < MIN_CHUNK_LEN) continue;
    chunks.push(s);
    offset = match.index + raw.length;
  }

  if (chunks.length > 0) {
    return { chunks, newSpokenUpTo: spokenUpTo + offset };
  }

  // No full sentence yet — speak at comma/semicolon if enough text accumulated
  const clause = slice.match(new RegExp(`^(.{${CLAUSE_BREAK_LEN},}?[,;])\\s+`));
  if (clause) {
    const s = clause[1].trim();
    if (s.length >= CLAUSE_BREAK_LEN) {
      return { chunks: [s], newSpokenUpTo: spokenUpTo + clause[0].length };
    }
  }

  return { chunks: [], newSpokenUpTo: spokenUpTo };
}

/** @deprecated use extractNewSpeechChunks */
export function extractNewSentences(buffer, spokenUpTo) {
  return extractNewSpeechChunks(buffer, spokenUpTo);
}

export function stripMarkdownForSpeech(text) {
  if (!text) return "";
  return text
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/!\[([^\]]*)\]\([^)]+\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/#{1,6}\s*/g, "")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/\*([^*]+)\*/g, "$1")
    .replace(/^[-*+]\s+/gm, "")
    .replace(/\s+/g, " ")
    .trim();
}

function speakInstantBrowser(text) {
  if (!window.speechSynthesis || !text) return false;
  window.speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.rate = 1.15;
  window.speechSynthesis.speak(u);
  return true;
}

/**
 * Manages listening (STT), speaking (TTS queue), and interrupt.
 */
export function createVoiceSession({
  voiceId = "en-US-AriaNeural",
  onTranscript,
  onListeningChange,
  onSpeakingChange,
  onError,
}) {
  let recognition = null;
  let listening = false;
  let voiceMode = false;

  let audio = null;
  let speechQueue = [];
  let processingQueue = false;
  let cancelled = false;
  let abortController = null;
  let prefetchPromise = null;
  let prefetchKey = null;
  let awaitingAssistant = false;
  let resumeTimer = null;

  function clearResumeTimer() {
    if (resumeTimer) {
      clearTimeout(resumeTimer);
      resumeTimer = null;
    }
  }

  /** Resume mic after reply + TTS are fully done, then LISTEN_RESUME_DELAY_MS pause. */
  function scheduleResumeListening() {
    if (!voiceMode || cancelled) return;
    clearResumeTimer();

    const attempt = () => {
      if (!voiceMode || cancelled) return;
      const stillBusy =
        awaitingAssistant ||
        processingQueue ||
        speechQueue.length > 0 ||
        audio !== null;
      if (stillBusy) {
        resumeTimer = setTimeout(attempt, 100);
        return;
      }
      resumeTimer = setTimeout(() => {
        resumeTimer = null;
        if (voiceMode && !cancelled && !listening && !awaitingAssistant) {
          startListening();
        }
      }, LISTEN_RESUME_DELAY_MS);
    };

    attempt();
  }

  function getRecognition() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) return null;
    const r = new SR();
    r.continuous = false;
    r.interimResults = true;
    r.lang = "en-US";
    r.maxAlternatives = 1;
    return r;
  }

  function stopAudio() {
    if (audio) {
      audio.pause();
      audio.src = "";
      audio = null;
    }
    window.speechSynthesis?.cancel();
  }

  function interrupt() {
    cancelled = true;
    speechQueue = [];
    processingQueue = false;
    prefetchPromise = null;
    prefetchKey = null;
    clearResumeTimer();
    stopAudio();
    if (abortController) {
      abortController.abort();
      abortController = null;
    }
    onSpeakingChange?.(false);
  }

  function newAbortSignal() {
    interrupt();
    cancelled = false;
    abortController = new AbortController();
    return abortController.signal;
  }

  async function fetchTts(text) {
    const res = await fetch(`${BASE}/voice/tts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, voice: voiceId }),
    });
    if (!res.ok) {
      const err = await res.text();
      throw new Error(err || `TTS HTTP ${res.status}`);
    }
    return res.blob();
  }

  function prefetchNext(text) {
    const clean = stripMarkdownForSpeech(text);
    if (!clean || cancelled) return;
    prefetchKey = clean;
    prefetchPromise = fetchTts(clean).catch(() => null);
  }

  function playBlob(blob) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(blob);
      audio = new Audio(url);
      audio.onended = () => {
        URL.revokeObjectURL(url);
        audio = null;
        resolve();
      };
      audio.onerror = () => {
        URL.revokeObjectURL(url);
        audio = null;
        reject(new Error("Audio playback failed"));
      };
      audio.play().catch(reject);
    });
  }

  async function processQueue() {
    if (processingQueue) return;
    processingQueue = true;
    onSpeakingChange?.(true);

    while (speechQueue.length > 0 && !cancelled) {
      const text = speechQueue.shift();
      const clean = stripMarkdownForSpeech(text);
      if (!clean) continue;

      if (speechQueue.length > 0) {
        prefetchNext(speechQueue[0]);
      }

      try {
        let blob = null;
        if (prefetchKey === clean && prefetchPromise) {
          blob = await prefetchPromise;
          prefetchPromise = null;
          prefetchKey = null;
        }
        if (!blob) {
          blob = await fetchTts(clean);
        }
        if (cancelled || !blob) break;
        await playBlob(blob);
      } catch (e) {
        console.warn("TTS:", e);
        onError?.(e.message || "Speech failed");
      }
    }

    prefetchPromise = null;
    prefetchKey = null;
    processingQueue = false;
    if (!cancelled) onSpeakingChange?.(false);
    scheduleResumeListening();
  }

  function enqueueSpeech(text) {
    if (!text?.trim() || cancelled) return;
    const wasEmpty = speechQueue.length === 0 && !processingQueue;
    speechQueue.push(text);
    if (wasEmpty) prefetchNext(text);
    processQueue();
  }

  function stopListening() {
    if (recognition) {
      try {
        recognition.stop();
      } catch (_) {
        /* ignore */
      }
      recognition = null;
    }
    listening = false;
    onListeningChange?.(false);
  }

  function startListening() {
    if (!voiceMode || listening || awaitingAssistant) return;
    const r = getRecognition();
    if (!r) {
      onError?.("Speech recognition not supported in this browser. Use Chrome or Edge.");
      return;
    }

    recognition = r;
    let finalText = "";

    r.onstart = () => {
      listening = true;
      onListeningChange?.(true);
    };

    r.onresult = (e) => {
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const t = e.results[i][0].transcript;
        if (e.results[i].isFinal) finalText += t;
      }
    };

    r.onend = () => {
      listening = false;
      onListeningChange?.(false);
      recognition = null;
      const text = finalText.trim();
      if (text && voiceMode && !cancelled) {
        awaitingAssistant = true;
        clearResumeTimer();
        onTranscript?.(text);
      } else if (voiceMode && !cancelled && !text) {
        scheduleResumeListening();
      }
    };

    r.onerror = (e) => {
      listening = false;
      onListeningChange?.(false);
      recognition = null;
      if (e.error === "aborted" || e.error === "no-speech") {
        if (voiceMode && !cancelled && !awaitingAssistant) {
          scheduleResumeListening();
        }
        return;
      }
      onError?.(e.error || "Microphone error");
    };

    try {
      r.start();
    } catch (e) {
      onError?.(e.message);
    }
  }

  function enterVoiceMode() {
    voiceMode = true;
    cancelled = false;
    awaitingAssistant = false;
    speechQueue = [];
    processingQueue = false;
    clearResumeTimer();
    stopAudio();
    abortController = null;
    startListening();
  }

  function exitVoiceMode() {
    voiceMode = false;
    awaitingAssistant = false;
    clearResumeTimer();
    stopListening();
    interrupt();
  }

  function toggleVoiceMode() {
    if (voiceMode) exitVoiceMode();
    else enterVoiceMode();
    return voiceMode;
  }

  function isVoiceMode() {
    return voiceMode;
  }

  function isListening() {
    return listening;
  }

  function isSpeaking() {
    return processingQueue || speechQueue.length > 0 || audio !== null;
  }

  /** Call when the chat stream finishes (success or error). */
  function notifyAssistantTurnComplete() {
    awaitingAssistant = false;
    scheduleResumeListening();
  }

  /** Short status while tools run — browser TTS for instant feedback. */
  function speakStatus(message) {
    if (!speakInstantBrowser(message)) {
      enqueueSpeech(message);
    }
  }

  return {
    enterVoiceMode,
    exitVoiceMode,
    toggleVoiceMode,
    isVoiceMode,
    isListening,
    isSpeaking,
    startListening,
    stopListening,
    interrupt,
    newAbortSignal,
    enqueueSpeech,
    speakStatus,
    notifyAssistantTurnComplete,
    scheduleResumeListening,
    setVoiceId: (id) => {
      voiceId = id || voiceId;
    },
  };
}
