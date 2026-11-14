/*
 * Runs before the SynapseAI page's own scripts, inside the Android app only.
 *
 * Android's WebView has no working Web Speech API, which the site's voice
 * mode and "Read aloud" use. This provides SpeechRecognition and
 * speechSynthesis on top of the phone's own speech recognizer and
 * text-to-speech (NativeVoice.java), so the page code runs unchanged.
 */
(function () {
  "use strict";
  var port = window.SynapseNative;
  if (!port || window.__synapseBridge) return;
  window.__synapseBridge = true;
  var flags = window.__SYNAPSE_NATIVE__ || {};

  var seq = 0;
  var handlers = {};
  function uid(prefix) { seq += 1; return prefix + seq + "-" + Date.now().toString(36); }
  function send(msg) { try { port.postMessage(JSON.stringify(msg)); } catch (e) { /* app gone */ } }

  function fire(target, type, props) {
    var ev = new Event(type);
    if (props) Object.keys(props).forEach(function (k) {
      try { Object.defineProperty(ev, k, { value: props[k], enumerable: true }); } catch (e) { /* ignore */ }
    });
    target.dispatchEvent(ev);
    var fn = target["on" + type];
    if (typeof fn === "function") { try { fn.call(target, ev); } catch (e) { setTimeout(function () { throw e; }); } }
  }

  port.onmessage = function (event) {
    var m;
    try { m = JSON.parse(event.data); } catch (e) { return; }
    if (m.t === "voices") { synth._setVoices(m.voices || []); return; }
    var h = m.id && handlers[m.id];
    if (h) h(m);
  };

  // ── SpeechRecognition ────────────────────────────────────────────────
  class NativeSpeechRecognition extends EventTarget {
    constructor() {
      super();
      this.lang = "";
      this.continuous = false;
      this.interimResults = false;
      this.maxAlternatives = 1;
      this.onstart = this.onend = this.onresult = this.onerror = null;
      this.onaudiostart = this.onaudioend = this.onspeechstart = this.onspeechend = this.onnomatch = null;
      this._id = null;
    }

    start() {
      if (this._id) throw new DOMException("Recognition has already started.", "InvalidStateError");
      var self = this;
      var id = (this._id = uid("sr"));
      handlers[id] = function (m) {
        if (m.t === "sr.start") {
          fire(self, "start");
        } else if (m.t === "sr.result") {
          var result = [{ transcript: m.text || "", confidence: m.final ? 0.9 : 0 }];
          result.isFinal = !!m.final;
          result.item = function (i) { return this[i]; };
          var results = [result];
          results.item = function (i) { return this[i]; };
          fire(self, "result", { resultIndex: 0, results: results });
        } else if (m.t === "sr.error") {
          fire(self, "error", { error: m.error, message: m.error });
        } else if (m.t === "sr.end") {
          delete handlers[id];
          self._id = null;
          fire(self, "end");
        }
      };
      send({ t: "sr.start", id: id, lang: this.lang || navigator.language || "en-US", interim: !!this.interimResults });
    }

    stop() { if (this._id) send({ t: "sr.stop", id: this._id }); }
    abort() { if (this._id) send({ t: "sr.abort", id: this._id }); }
  }

  // The WebView's own constructor exists on some versions but never works.
  var SR = flags.sr ? NativeSpeechRecognition : undefined;
  ["SpeechRecognition", "webkitSpeechRecognition"].forEach(function (name) {
    try { Object.defineProperty(window, name, { value: SR, configurable: true, writable: true }); } catch (e) { /* ignore */ }
  });

  // ── speechSynthesis ──────────────────────────────────────────────────
  class NativeUtterance extends EventTarget {
    constructor(text) {
      super();
      this.text = text == null ? "" : String(text);
      this.lang = "";
      this.voice = null;
      this.volume = 1;
      this.rate = 1;
      this.pitch = 1;
      this.onstart = this.onend = this.onerror = this.onpause = this.onresume = this.onboundary = this.onmark = null;
    }
  }

  class NativeSynthesis extends EventTarget {
    constructor() {
      super();
      this.onvoiceschanged = null;
      this._voices = [];
      this._queue = [];   // [{ id, u, started }]
    }
    get speaking() { return this._queue.some(function (e) { return e.started; }); }
    get pending() { return this._queue.some(function (e) { return !e.started; }); }
    get paused() { return false; }
    getVoices() { return this._voices.slice(); }

    speak(u) {
      var self = this;
      var entry = { id: uid("tts"), u: u, started: false };
      this._queue.push(entry);
      handlers[entry.id] = function (m) {
        if (m.t === "tts.start") {
          entry.started = true;
          fire(u, "start");
        } else if (m.t === "tts.end" || m.t === "tts.error") {
          self._drop(entry.id);
          if (m.t === "tts.end") fire(u, "end");
          else fire(u, "error", { error: "synthesis-failed" });
        }
      };
      send({
        t: "tts.speak",
        id: entry.id,
        text: u.text,
        lang: u.lang || (u.voice && u.voice.lang) || "",
        voice: u.voice ? u.voice.name : "",
        rate: Number(u.rate) || 1,
        pitch: Number(u.pitch) || 1,
      });
    }

    // Like stopping in a browser, but cancelled utterances report "end" rather
    // than an error, so the page doesn't show "Speech failed" when you stop it.
    cancel() {
      var cancelled = this._queue;
      this._queue = [];
      send({ t: "tts.cancel" });
      cancelled.forEach(function (e) { delete handlers[e.id]; fire(e.u, "end"); });
    }
    pause() {}
    resume() {}

    _drop(id) {
      delete handlers[id];
      this._queue = this._queue.filter(function (e) { return e.id !== id; });
    }
    _setVoices(list) {
      this._voices = list.map(function (v) {
        return Object.freeze({ name: v.name, lang: v.lang, voiceURI: v.name, localService: !!v.local, default: !!v.def });
      });
      fire(this, "voiceschanged");
    }
  }

  var synth = new NativeSynthesis();
  try { Object.defineProperty(window, "speechSynthesis", { value: synth, configurable: true, writable: true }); } catch (e) { /* ignore */ }
  try { Object.defineProperty(window, "SpeechSynthesisUtterance", { value: NativeUtterance, configurable: true, writable: true }); } catch (e) { /* ignore */ }

  send({ t: "hello" });
})();
