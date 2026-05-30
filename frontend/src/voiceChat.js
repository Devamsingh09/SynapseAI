const BASE = process.env.REACT_APP_API_URL || "http://localhost:8000";

export function isVoiceSupported() {
  return !!(
    (typeof navigator !== "undefined" &&
      navigator.mediaDevices &&
      navigator.mediaDevices.getUserMedia) ||
    (typeof window !== "undefined" &&
      (window.SpeechRecognition || window.webkitSpeechRecognition))
  );
}

export function hasBrowserSpeechRecognition() {
  return !!(
    typeof window !== "undefined" &&
    (window.SpeechRecognition || window.webkitSpeechRecognition)
  );
}

export const VOICE_PAUSE_MS = 2000;
export const INTERRUPT_HOLD_MS = 350;
export const INTERRUPT_THRESHOLD = 0.022;

export function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

const SENTENCE_RE = /[^.!?]+[.!?]+(?:\s|$)|[^.!?]+$/g;

export function cleanTextForSpeech(text) {
  if (!text) return "";
  return text
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`[^`]+`/g, " ")
    .replace(/!\[[^\]]*\]\([^)]+\)/g, " ")
    .replace(/\[[^\]]+\]\([^)]+\)/g, " ")
    .replace(/https?:\/\/\S+/g, "link")
    .replace(/[#*_~>|]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

export function extractNewSentences(buffer, spokenUpTo) {
  const slice = buffer.slice(spokenUpTo);
  const sentences = [];
  let offset = 0;
  SENTENCE_RE.lastIndex = 0;

  let match;
  while ((match = SENTENCE_RE.exec(slice)) !== null) {
    const raw = match[0];
    const s = raw.trim();
    if (!/[.!?]$/.test(s)) break;
    if (s.length < 8 && sentences.length === 0) continue;
    sentences.push(s);
    offset = match.index + raw.length;
  }

  return { sentences, newSpokenUpTo: spokenUpTo + offset };
}

export class SpeechQueue {
  constructor() {
    this.queue = [];
    this.playing = false;
    this.currentAudio = null;
    this.stopped = false;
    this.preferredVoice = null;
    this._finishSpeak = null;
    this._loadVoice();
  }

  _loadVoice() {
    if (!("speechSynthesis" in window)) return;
    const pick = () => {
      const voices = window.speechSynthesis.getVoices();
      this.preferredVoice =
        voices.find(v => /Google US English|Microsoft (Aria|Jenny|Guy)/i.test(v.name)) ||
        voices.find(v => v.lang.startsWith("en") && v.localService) ||
        voices.find(v => v.lang.startsWith("en")) ||
        null;
    };
    pick();
    window.speechSynthesis.onvoiceschanged = pick;
  }

  async speak(text) {
    const cleaned = cleanTextForSpeech(text);
    if (!cleaned || this.stopped) return;
    this.queue = [cleaned];
    if (!this.playing) await this._drain();
    await this._waitUntilIdle();
  }

  /** Stop playback immediately (barge-in). */
  interrupt() {
    this.queue = [];
    this.stopped = true;
    if (this.currentAudio) {
      this.currentAudio.pause();
      this.currentAudio.currentTime = 0;
      this.currentAudio = null;
    }
    if ("speechSynthesis" in window) window.speechSynthesis.cancel();
    if (this._finishSpeak) {
      this._finishSpeak();
      this._finishSpeak = null;
    }
    this.playing = false;
    this.stopped = false;
  }

  async enqueue(text) {
    const t = cleanTextForSpeech(text);
    if (!t || this.stopped) return;
    this.queue.push(t);
    if (!this.playing) await this._drain();
  }

  async flushRemainder(text) {
    const t = cleanTextForSpeech(text);
    if (t && !this.stopped) {
      this.queue.push(t);
      if (!this.playing) await this._drain();
    }
    await this._waitUntilIdle();
  }

  _waitUntilIdle() {
    if (!this.playing) return Promise.resolve();
    return new Promise(resolve => {
      const check = () => {
        if (!this.playing && this.queue.length === 0) resolve();
        else requestAnimationFrame(check);
      };
      check();
    });
  }

  async _drain() {
    this.playing = true;
    while (this.queue.length && !this.stopped) {
      await this._playOne(this.queue.shift());
    }
    this.playing = false;
  }

  async _playOne(text) {
    if (this.stopped) return;
    // Fast path: browser TTS (zero network latency)
    if ("speechSynthesis" in window) {
      try {
        await this._browserSpeak(text);
        return;
      } catch {
        // fall through to backend
      }
    }
    await this._edgeSpeak(text);
  }

  async _edgeSpeak(text) {
    const res = await fetch(`${BASE}/voice/speak`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!res.ok) throw new Error(`TTS HTTP ${res.status}`);

    const blob = await res.blob();
    if (!blob.size || this.stopped) return;

    await new Promise((resolve, reject) => {
      const url = URL.createObjectURL(blob);
      const audio = new Audio(url);
      this.currentAudio = audio;
      audio.onended = () => {
        URL.revokeObjectURL(url);
        this.currentAudio = null;
        resolve();
      };
      audio.onerror = () => {
        URL.revokeObjectURL(url);
        this.currentAudio = null;
        reject(new Error("Audio playback failed"));
      };
      audio.play().catch(reject);
    });
  }

  _browserSpeak(text) {
    if (!("speechSynthesis" in window) || this.stopped) return Promise.resolve();
    return new Promise((resolve) => {
      window.speechSynthesis.cancel();
      const u = new SpeechSynthesisUtterance(text);
      u.rate = 1.28;
      u.pitch = 1;
      if (this.preferredVoice) u.voice = this.preferredVoice;
      const done = () => {
        if (this._finishSpeak === done) this._finishSpeak = null;
        resolve();
      };
      this._finishSpeak = done;
      u.onend = done;
      u.onerror = done;
      window.speechSynthesis.speak(u);
    });
  }

  stop() {
    this.interrupt();
  }

  reset() {
    this.stopped = false;
    this.queue = [];
  }
}

/** Detect user speech while bot is speaking/thinking — triggers barge-in. */
export class InterruptWatcher {
  constructor({
    onInterrupt,
    threshold = INTERRUPT_THRESHOLD,
    holdMs = INTERRUPT_HOLD_MS,
  } = {}) {
    this.onInterrupt = onInterrupt;
    this.threshold = threshold;
    this.holdMs = holdMs;
    this.active = false;
    this.speechStart = null;
    this.triggered = false;
  }

  async start() {
    if (this.active) return;
    this.active = true;
    this.triggered = false;
    this.speechStart = null;

    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      this.audioContext = new AudioContext();
      if (this.audioContext.state === "suspended") await this.audioContext.resume();
      this.analyser = this.audioContext.createAnalyser();
      this.analyser.fftSize = 2048;
      this.audioContext.createMediaStreamSource(this.stream).connect(this.analyser);
      this._poll();
    } catch {
      this.active = false;
    }
  }

  _getVolume() {
    const data = new Uint8Array(this.analyser.fftSize);
    this.analyser.getByteTimeDomainData(data);
    let sum = 0;
    for (let i = 0; i < data.length; i++) {
      const v = (data[i] - 128) / 128;
      sum += v * v;
    }
    return Math.sqrt(sum / data.length);
  }

  _poll() {
    if (!this.active || this.triggered) return;

    const rms = this._getVolume();
    if (rms > this.threshold) {
      if (!this.speechStart) this.speechStart = Date.now();
      else if (Date.now() - this.speechStart >= this.holdMs) {
        this.triggered = true;
        this.stop();
        this.onInterrupt?.();
        return;
      }
    } else {
      this.speechStart = null;
    }

    this.rafId = requestAnimationFrame(() => this._poll());
  }

  stop() {
    this.active = false;
    if (this.rafId) cancelAnimationFrame(this.rafId);
    this.stream?.getTracks().forEach(t => t.stop());
    if (this.audioContext?.state !== "closed") {
      this.audioContext.close().catch(() => {});
    }
  }
}

/** Fast STT — Web Speech API with continuous + interim results. */
export class BrowserSpeechListener {
  constructor({ onResult, onError, onRestart, onSpeechStart, lang = "en-US", silenceMs = VOICE_PAUSE_MS } = {}) {
    this.onResult = onResult;
    this.onError = onError;
    this.onRestart = onRestart;
    this.onSpeechStart = onSpeechStart;
    this.lang = lang;
    this.silenceMs = silenceMs;
    this.active = false;
    this.recognition = null;
    this.handled = false;
    this.latestText = "";
    this.silenceTimer = null;
  }

  _clearSilenceTimer() {
    if (this.silenceTimer) {
      clearTimeout(this.silenceTimer);
      this.silenceTimer = null;
    }
  }

  _scheduleFinalize() {
    this._clearSilenceTimer();
    this.silenceTimer = setTimeout(() => this._finalize(), this.silenceMs);
  }

  _finalize() {
    if (!this.active || this.handled || !this.latestText.trim()) return;
    this.handled = true;
    this.active = false;
    this._clearSilenceTimer();
    const text = this.latestText.trim();
    this.latestText = "";
    try {
      this.recognition?.stop();
    } catch (_) {}
    this.onResult?.(text);
  }

  _attachHandlers(r) {
    r.onresult = (e) => {
      if (!this.active || this.handled) return;
      let chunk = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        chunk += e.results[i][0].transcript;
      }
      const text = chunk.trim();
      if (!text) return;
      if (!this.latestText) this.onSpeechStart?.();
      this.latestText = text;
      this._scheduleFinalize();
    };

    r.onerror = (e) => {
      const err = e.error || "speech-error";
      if (err === "aborted") return;
      if (err === "no-speech" || err === "audio-capture") {
        if (this.active && !this.handled) this.onRestart?.();
        return;
      }
      this.onError?.(new Error(err));
    };

    r.onend = () => {
      if (this.active && !this.handled) {
        if (this.latestText.trim()) {
          this._finalize();
        } else {
          this.onRestart?.();
        }
      }
    };
  }

  start() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) throw new Error("Speech recognition not supported");

    this.active = true;
    this.handled = false;
    this.latestText = "";

    const r = new SR();
    r.lang = this.lang;
    r.interimResults = true;
    r.continuous = true;
    r.maxAlternatives = 1;
    this._attachHandlers(r);
    this.recognition = r;

    try {
      r.start();
    } catch (err) {
      this.active = false;
      this.onError?.(err);
    }
  }

  stop() {
    this.active = false;
    this.handled = true;
    this._clearSilenceTimer();
    this.latestText = "";
    try {
      this.recognition?.abort();
    } catch (_) {
      try { this.recognition?.stop(); } catch (_) {}
    }
    this.recognition = null;
  }
}

/** Fallback listener — MediaRecorder + silence detection. */
export class VoiceRecorder {
  constructor({
    silenceMs = VOICE_PAUSE_MS,
    maxMs = 30000,
    threshold = 0.008,
    onSilence,
    onMaxDuration,
    onError,
  } = {}) {
    this.silenceMs = silenceMs;
    this.maxMs = maxMs;
    this.threshold = threshold;
    this.onSilence = onSilence;
    this.onMaxDuration = onMaxDuration;
    this.onError = onError;
    this.active = false;
  }

  async start() {
    if (this.active) return;
    this.chunks = [];
    this.hasSpeech = false;
    this.silenceStart = null;
    this.active = true;
    this.startTime = Date.now();

    try {
      this.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      this.audioContext = new AudioContext();
      if (this.audioContext.state === "suspended") {
        await this.audioContext.resume();
      }

      this.analyser = this.audioContext.createAnalyser();
      this.analyser.fftSize = 2048;
      this.audioContext.createMediaStreamSource(this.stream).connect(this.analyser);

      const mimeTypes = [
        "audio/webm;codecs=opus",
        "audio/webm",
        "audio/mp4",
        "audio/ogg;codecs=opus",
      ];
      const mime = mimeTypes.find(t => MediaRecorder.isTypeSupported(t)) || "";

      this.mediaRecorder = mime
        ? new MediaRecorder(this.stream, { mimeType: mime })
        : new MediaRecorder(this.stream);

      this.mediaRecorder.ondataavailable = e => {
        if (e.data.size) this.chunks.push(e.data);
      };
      this.mediaRecorder.start(250);
      this._pollVolume();
    } catch (err) {
      this.active = false;
      this.onError?.(err);
      throw err;
    }
  }

  _getVolume() {
    const data = new Uint8Array(this.analyser.fftSize);
    this.analyser.getByteTimeDomainData(data);
    let sum = 0;
    for (let i = 0; i < data.length; i++) {
      const v = (data[i] - 128) / 128;
      sum += v * v;
    }
    return Math.sqrt(sum / data.length);
  }

  _pollVolume() {
    if (!this.active) return;

    if (Date.now() - this.startTime > this.maxMs) {
      this._finish(this.onMaxDuration);
      return;
    }

    const rms = this._getVolume();
    if (rms > this.threshold) {
      this.hasSpeech = true;
      this.silenceStart = null;
    } else if (this.hasSpeech) {
      if (!this.silenceStart) this.silenceStart = Date.now();
      else if (Date.now() - this.silenceStart >= this.silenceMs) {
        this._finish(this.onSilence);
        return;
      }
    }

    this.rafId = requestAnimationFrame(() => this._pollVolume());
  }

  async _finish(callback) {
    if (!this.active) return;
    this.active = false;
    if (this.rafId) cancelAnimationFrame(this.rafId);

    const blob = await new Promise(resolve => {
      if (!this.mediaRecorder || this.mediaRecorder.state === "inactive") {
        resolve(this._makeBlob());
        return;
      }
      this.mediaRecorder.onstop = () => resolve(this._makeBlob());
      try {
        this.mediaRecorder.stop();
      } catch {
        resolve(this._makeBlob());
      }
    });

    this.stream?.getTracks().forEach(t => t.stop());
    if (this.audioContext?.state !== "closed") {
      await this.audioContext.close().catch(() => {});
    }

    callback?.(blob);
  }

  _makeBlob() {
    const type = this.mediaRecorder?.mimeType || "audio/webm";
    return new Blob(this.chunks, { type });
  }

  stop() {
    this.active = false;
    if (this.rafId) cancelAnimationFrame(this.rafId);
    if (this.mediaRecorder?.state === "recording") {
      try { this.mediaRecorder.stop(); } catch (_) {}
    }
    this.stream?.getTracks().forEach(t => t.stop());
    if (this.audioContext?.state !== "closed") {
      this.audioContext.close().catch(() => {});
    }
  }
}

export async function transcribeBlob(blob) {
  const form = new FormData();
  form.append("audio", blob, "recording.webm");

  const res = await fetch(`${BASE}/voice/transcribe`, { method: "POST", body: form });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    const detail = err.detail;
    throw new Error(
      typeof detail === "string" ? detail : `Transcription failed (${res.status})`
    );
  }
  const data = await res.json();
  return (data.text || "").trim();
}

export async function transcribeWithFallback(blob) {
  if (blob?.size > 500) {
    try {
      return await transcribeBlob(blob);
    } catch {
      // fall through to browser if blob path fails
    }
  }
  return "";
}

export async function checkVoiceStatus() {
  try {
    const res = await fetch(`${BASE}/voice/status`);
    if (!res.ok) return { stt: false, tts: false };
    return res.json();
  } catch {
    return { stt: false, tts: false };
  }
}
