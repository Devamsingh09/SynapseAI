/**
 * Voice layer for Synapse AI — same chat pipeline as text (POST /chat/stream).
 * STT: Web Speech API (browser)
 * TTS: Web Speech Synthesis API (browser) — fast, works on Vercel/HF deploy, no backend call
 */

const SENTENCE_RE = /[^.!?]+[.!?]+(?:\s|$)/g;
const MIN_CHUNK_LEN = 6;
const CLAUSE_BREAK_LEN = 40;
const LISTEN_RESUME_DELAY_MS = 1200;

let preferredVoice = null;

function loadPreferredVoice() {
  if (!window.speechSynthesis) return;
  const voices = window.speechSynthesis.getVoices();
  preferredVoice =
    voices.find((v) => /google uk english female|google us english/i.test(v.name)) ||
    voices.find((v) => /microsoft (zira|aria|jenny|natural)/i.test(v.name)) ||
    voices.find((v) => v.lang === "en-IN") ||
    voices.find((v) => v.lang === "en-US") ||
    voices.find((v) => v.lang.startsWith("en")) ||
    null;
}

if (typeof window !== "undefined" && window.speechSynthesis) {
  loadPreferredVoice();
  window.speechSynthesis.onvoiceschanged = loadPreferredVoice;
}

export function isVoiceSupported() {
  return (
    !!(window.SpeechRecognition || window.webkitSpeechRecognition) &&
    !!window.speechSynthesis
  );
}

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

  const clause = slice.match(new RegExp(`^(.{${CLAUSE_BREAK_LEN},}?[,;])\\s+`));
  if (clause) {
    const s = clause[1].trim();
    if (s.length >= CLAUSE_BREAK_LEN) {
      return { chunks: [s], newSpokenUpTo: spokenUpTo + clause[0].length };
    }
  }

  return { chunks: [], newSpokenUpTo: spokenUpTo };
}

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

function isSynthSpeaking() {
  return !!window.speechSynthesis?.speaking || !!window.speechSynthesis?.pending;
}

function stopSpeaking() {
  window.speechSynthesis?.cancel();
}

function speakBrowser(text) {
  return new Promise((resolve, reject) => {
    if (!window.speechSynthesis) {
      reject(new Error("Speech synthesis not supported in this browser."));
      return;
    }
    const clean = stripMarkdownForSpeech(text);
    if (!clean) {
      resolve();
      return;
    }
    loadPreferredVoice();
    const u = new SpeechSynthesisUtterance(clean);
    u.rate = 1.12;
    u.pitch = 1;
    u.lang = preferredVoice?.lang || "en-US";
    if (preferredVoice) u.voice = preferredVoice;
    u.onend = () => resolve();
    u.onerror = () => reject(new Error("Speech playback failed"));
    window.speechSynthesis.speak(u);
  });
}

export function createVoiceSession({
  onTranscript,
  onListeningChange,
  onSpeakingChange,
  onError,
}) {
  let recognition = null;
  let listening = false;
  let voiceMode = false;
  let speechQueue = [];
  let processingQueue = false;
  let cancelled = false;
  let abortController = null;
  let awaitingAssistant = false;
  let resumeTimer = null;

  function clearResumeTimer() {
    if (resumeTimer) {
      clearTimeout(resumeTimer);
      resumeTimer = null;
    }
  }

  function scheduleResumeListening() {
    if (!voiceMode || cancelled) return;
    clearResumeTimer();

    const attempt = () => {
      if (!voiceMode || cancelled) return;
      const stillBusy =
        awaitingAssistant ||
        processingQueue ||
        speechQueue.length > 0 ||
        isSynthSpeaking();
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

  function interrupt() {
    cancelled = true;
    speechQueue = [];
    processingQueue = false;
    clearResumeTimer();
    stopSpeaking();
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

  async function processQueue() {
    if (processingQueue) return;
    processingQueue = true;
    onSpeakingChange?.(true);

    while (speechQueue.length > 0 && !cancelled) {
      const text = speechQueue.shift();
      try {
        await speakBrowser(text);
      } catch (e) {
        console.warn("TTS:", e);
        onError?.(e.message || "Speech failed");
      }
    }

    processingQueue = false;
    if (!cancelled) onSpeakingChange?.(false);
    scheduleResumeListening();
  }

  function enqueueSpeech(text) {
    if (!text?.trim() || cancelled) return;
    speechQueue.push(text);
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
      onError?.("Speech recognition not supported. Use Chrome or Edge.");
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
    stopSpeaking();
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
    return processingQueue || speechQueue.length > 0 || isSynthSpeaking();
  }

  function notifyAssistantTurnComplete() {
    awaitingAssistant = false;
    scheduleResumeListening();
  }

  function speakStatus(message) {
    speakBrowser(message).catch(() => {});
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
  };
}
