const BASE = process.env.REACT_APP_API_URL || "http://localhost:8000";

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

export class VoiceRecorder {
  constructor({
    silenceMs = 1400,
    maxMs = 45000,
    threshold = 0.018,
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
      this.analyser = this.audioContext.createAnalyser();
      this.analyser.fftSize = 512;
      this.audioContext.createMediaStreamSource(this.stream).connect(this.analyser);

      const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
        ? "audio/webm;codecs=opus"
        : "audio/webm";

      this.mediaRecorder = new MediaRecorder(this.stream, { mimeType: mime });
      this.mediaRecorder.ondataavailable = e => {
        if (e.data.size) this.chunks.push(e.data);
      };
      this.mediaRecorder.start(120);
      this._pollVolume();
    } catch (err) {
      this.active = false;
      this.onError?.(err);
      throw err;
    }
  }

  _pollVolume() {
    if (!this.active) return;

    if (Date.now() - this.startTime > this.maxMs) {
      this._finish(this.onMaxDuration);
      return;
    }

    const data = new Uint8Array(this.analyser.frequencyBinCount);
    this.analyser.getByteFrequencyData(data);
    const avg = data.reduce((a, b) => a + b, 0) / data.length / 255;

    if (avg > this.threshold) {
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

  _finish(callback) {
    if (!this.active) return;
    const blob = this.getBlob();
    this.stop();
    callback?.(blob);
  }

  getBlob() {
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
      this.audioContext?.close().catch(() => {});
    }
  }
}

export async function transcribeBlob(blob) {
  const form = new FormData();
  form.append("audio", blob, "recording.webm");

  const res = await fetch(`${BASE}/voice/transcribe`, { method: "POST", body: form });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Transcription failed (${res.status})`);
  }
  const data = await res.json();
  return (data.text || "").trim();
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

export function browserTranscribe() {
  return new Promise((resolve, reject) => {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
      reject(new Error("Speech recognition not supported in this browser"));
      return;
    }
    const r = new SR();
    r.lang = "en-US";
    r.interimResults = false;
    r.onresult = e => resolve(e.results[0][0].transcript.trim());
    r.onerror = e => reject(new Error(e.error || "Speech recognition failed"));
    r.start();
  });
}

export async function transcribeWithFallback(blob) {
  try {
    return await transcribeBlob(blob);
  } catch {
    return browserTranscribe();
  }
}
