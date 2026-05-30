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

const SENTENCE_RE = /[^.!?]+[.!?]+(?:\s|$)|[^.!?]+$/g;
const MIN_SENTENCE_LEN = 12;

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
    if (s.length < MIN_SENTENCE_LEN && sentences.length === 0) continue;
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
  }

  async enqueue(text) {
    const t = text?.trim();
    if (!t || this.stopped) return;
    this.queue.push(t);
    if (!this.playing) await this._drain();
  }

  async flushRemainder(text) {
    const t = text?.trim();
    if (t && !this.stopped) {
      this.queue.push(t);
      if (!this.playing) await this._drain();
    } else if (this.playing) {
      await this._waitUntilIdle();
    }
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
    try {
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
    } catch {
      await this._browserSpeak(text);
    }
  }

  _browserSpeak(text) {
    if (!("speechSynthesis" in window) || this.stopped) return Promise.resolve();
    return new Promise(resolve => {
      const u = new SpeechSynthesisUtterance(text);
      u.rate = 1.05;
      u.onend = resolve;
      u.onerror = resolve;
      window.speechSynthesis.speak(u);
    });
  }

  stop() {
    this.stopped = true;
    this.queue = [];
    if (this.currentAudio) {
      this.currentAudio.pause();
      this.currentAudio = null;
    }
    if ("speechSynthesis" in window) window.speechSynthesis.cancel();
    this.playing = false;
  }

  reset() {
    this.stopped = false;
  }
}

/** Primary listener — Web Speech API (reliable in Chrome/Edge). */
export class BrowserSpeechListener {
  constructor({ onResult, onError, onRestart, lang = "en-US" } = {}) {
    this.onResult = onResult;
    this.onError = onError;
    this.onRestart = onRestart;
    this.lang = lang;
    this.active = false;
    this.recognition = null;
    this.handled = false;
  }

  start() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) throw new Error("Speech recognition not supported");

    this.active = true;
    this.handled = false;
    const r = new SR();
    r.lang = this.lang;
    r.interimResults = false;
    r.continuous = false;
    r.maxAlternatives = 1;

    r.onresult = (e) => {
      const text = e.results?.[0]?.[0]?.transcript?.trim();
      if (!text || !this.active) return;
      this.handled = true;
      this.active = false;
      this.onResult?.(text);
    };

    r.onerror = (e) => {
      const err = e.error || "speech-error";
      if (err === "aborted") return;
      if (err === "no-speech" || err === "audio-capture") {
        if (this.active) this.onRestart?.();
        return;
      }
      this.onError?.(new Error(err));
    };

    r.onend = () => {
      if (this.active && !this.handled) {
        this.onRestart?.();
      }
    };

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
    silenceMs = 1200,
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
