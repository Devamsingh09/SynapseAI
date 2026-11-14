package com.synapseai.android;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.os.Bundle;
import android.speech.RecognitionListener;
import android.speech.RecognizerIntent;
import android.speech.SpeechRecognizer;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;
import android.speech.tts.Voice;

import androidx.webkit.JavaScriptReplyProxy;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.function.Consumer;

/**
 * Android speech recognition and text-to-speech for the page, spoken to over
 * one JSON message channel (see assets/bridge.js for the other side).
 *
 * Page → app:  hello · sr.start {id, lang, interim} · sr.stop {id} · sr.abort {id}
 *              tts.speak {id, text, lang, voice, rate, pitch} · tts.cancel
 * App → page:  voices {voices} · sr.start/sr.result {final, text}/sr.error {error}/sr.end
 *              tts.start/tts.end/tts.error
 */
final class NativeVoice implements TextToSpeech.OnInitListener {

    interface MicGate {
        void withMic(Consumer<Boolean> then);
    }

    private static final int TTS_PENDING = 0, TTS_READY = 1, TTS_FAILED = -1;

    private final Activity activity;
    private final MicGate mic;
    private JavaScriptReplyProxy page;

    private SpeechRecognizer recognizer;
    private String recognitionId;

    private TextToSpeech tts;
    private int ttsState = TTS_PENDING;
    private final List<JSONObject> ttsBacklog = new ArrayList<>();

    NativeVoice(Activity activity, MicGate mic) {
        this.activity = activity;
        this.mic = mic;
        this.tts = new TextToSpeech(activity.getApplicationContext(), this);
    }

    static boolean recognitionAvailable(Context context) {
        return SpeechRecognizer.isRecognitionAvailable(context);
    }

    /** Called on the UI thread for each message from the page. */
    void onMessage(String data, JavaScriptReplyProxy reply) {
        page = reply;
        JSONObject m;
        try {
            m = new JSONObject(data);
        } catch (JSONException e) {
            return;
        }
        String id = m.optString("id");
        switch (m.optString("t")) {
            case "hello":
                sendVoices();
                break;
            case "sr.start":
                startRecognition(id, m.optString("lang", "en-US"), m.optBoolean("interim", true));
                break;
            case "sr.stop":
                if (recognizer != null && id.equals(recognitionId)) recognizer.stopListening();
                break;
            case "sr.abort":
                if (id.equals(recognitionId)) endRecognition(id, "aborted");
                break;
            case "tts.speak":
                speak(m);
                break;
            case "tts.cancel":
                ttsBacklog.clear();
                if (tts != null && ttsState == TTS_READY) tts.stop();
                break;
            default:
                break;
        }
    }

    // ── Speech recognition ────────────────────────────────────────────────

    private void startRecognition(String id, String lang, boolean interim) {
        if (recognitionId != null) endRecognition(recognitionId, "aborted");
        recognitionId = id;
        mic.withMic(granted -> {
            if (!id.equals(recognitionId)) return;          // stopped or replaced meanwhile
            if (!granted) {
                endRecognition(id, "not-allowed");
                return;
            }
            recognizer = SpeechRecognizer.createSpeechRecognizer(activity);
            recognizer.setRecognitionListener(new Listener(id));
            Intent intent = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
                    .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
                    .putExtra(RecognizerIntent.EXTRA_LANGUAGE, lang.isEmpty() ? "en-US" : lang)
                    .putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, interim)
                    .putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
                    .putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, activity.getPackageName());
            recognizer.startListening(intent);
        });
    }

    /** Ends session {@code id}: optional error, then "end" — the order browsers use. */
    private void endRecognition(String id, String error) {
        if (!id.equals(recognitionId)) return;
        recognitionId = null;
        if (recognizer != null) {
            recognizer.cancel();
            recognizer.destroy();
            recognizer = null;
        }
        if (error != null) emit(event("sr.error", id).put("error", error));
        emit(event("sr.end", id));
    }

    private final class Listener implements RecognitionListener {
        private final String id;

        Listener(String id) {
            this.id = id;
        }

        @Override
        public void onReadyForSpeech(Bundle params) {
            if (id.equals(recognitionId)) emit(event("sr.start", id));
        }

        @Override
        public void onPartialResults(Bundle partial) {
            String text = firstResult(partial);
            if (id.equals(recognitionId) && !text.isEmpty()) {
                emit(event("sr.result", id).put("final", false).put("text", text));
            }
        }

        @Override
        public void onResults(Bundle results) {
            if (!id.equals(recognitionId)) return;
            String text = firstResult(results);
            if (text.isEmpty()) {
                endRecognition(id, "no-speech");
                return;
            }
            emit(event("sr.result", id).put("final", true).put("text", text));
            endRecognition(id, null);
        }

        @Override
        public void onError(int error) {
            endRecognition(id, webError(error));
        }

        @Override public void onBeginningOfSpeech() { }
        @Override public void onRmsChanged(float rmsdB) { }
        @Override public void onBufferReceived(byte[] buffer) { }
        @Override public void onEndOfSpeech() { }
        @Override public void onEvent(int eventType, Bundle params) { }
    }

    private static String firstResult(Bundle b) {
        ArrayList<String> list = b == null ? null : b.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION);
        return list == null || list.isEmpty() || list.get(0) == null ? "" : list.get(0).trim();
    }

    /** Android recognizer error → the Web Speech API error names the page expects. */
    private static String webError(int code) {
        switch (code) {
            case SpeechRecognizer.ERROR_NO_MATCH:
            case SpeechRecognizer.ERROR_SPEECH_TIMEOUT:
                return "no-speech";
            case SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS:
                return "not-allowed";
            case SpeechRecognizer.ERROR_NETWORK:
            case SpeechRecognizer.ERROR_NETWORK_TIMEOUT:
            case SpeechRecognizer.ERROR_SERVER:
                return "network";
            case SpeechRecognizer.ERROR_AUDIO:
                return "audio-capture";
            case 12:   // ERROR_LANGUAGE_NOT_SUPPORTED (API 31)
            case 13:   // ERROR_LANGUAGE_UNAVAILABLE (API 31)
                return "language-not-supported";
            default:   // client, busy, server disconnected, too many requests
                return "aborted";
        }
    }

    // ── Text-to-speech ────────────────────────────────────────────────────

    @Override
    public void onInit(int status) {
        activity.runOnUiThread(() -> {
            ttsState = status == TextToSpeech.SUCCESS ? TTS_READY : TTS_FAILED;
            if (ttsState == TTS_READY) {
                tts.setOnUtteranceProgressListener(new Progress());
                sendVoices();
            }
            List<JSONObject> waiting = new ArrayList<>(ttsBacklog);
            ttsBacklog.clear();
            for (JSONObject m : waiting) speak(m);
        });
    }

    private void speak(JSONObject m) {
        String id = m.optString("id");
        if (ttsState == TTS_PENDING) {
            ttsBacklog.add(m);
            return;
        }
        if (ttsState == TTS_FAILED) {
            emit(event("tts.error", id));
            return;
        }
        Voice voice = findVoice(m.optString("voice"));
        if (voice != null) {
            tts.setVoice(voice);
        } else {
            String lang = m.optString("lang");
            Locale locale = lang.isEmpty() ? Locale.getDefault() : Locale.forLanguageTag(lang);
            if (tts.isLanguageAvailable(locale) >= TextToSpeech.LANG_AVAILABLE) tts.setLanguage(locale);
        }
        tts.setSpeechRate((float) clamp(m.optDouble("rate", 1), 0.1, 4));
        tts.setPitch((float) clamp(m.optDouble("pitch", 1), 0.1, 2));
        if (tts.speak(m.optString("text"), TextToSpeech.QUEUE_ADD, null, id) != TextToSpeech.SUCCESS) {
            emit(event("tts.error", id));
        }
    }

    private Voice findVoice(String name) {
        if (name == null || name.isEmpty()) return null;
        Set<Voice> voices = installedVoices();
        for (Voice v : voices) if (v.getName().equals(name)) return v;
        return null;
    }

    private Set<Voice> installedVoices() {
        try {
            Set<Voice> voices = tts.getVoices();
            return voices == null ? Collections.emptySet() : voices;
        } catch (RuntimeException e) {   // some engines throw while (re)binding
            return Collections.emptySet();
        }
    }

    private void sendVoices() {
        if (ttsState != TTS_READY || page == null) return;
        Voice def = null;
        try {
            def = tts.getDefaultVoice();
        } catch (RuntimeException ignored) {
        }
        List<Voice> list = new ArrayList<>();
        for (Voice v : installedVoices()) {
            Set<String> features = v.getFeatures();
            if (features != null && features.contains(TextToSpeech.Engine.KEY_FEATURE_NOT_INSTALLED)) continue;
            list.add(v);
        }
        final Voice defaultVoice = def;
        // Default voice first, then voices that work offline, then by name.
        list.sort((a, b) -> {
            if (a.equals(defaultVoice) != b.equals(defaultVoice)) return a.equals(defaultVoice) ? -1 : 1;
            if (a.isNetworkConnectionRequired() != b.isNetworkConnectionRequired()) {
                return a.isNetworkConnectionRequired() ? 1 : -1;
            }
            return a.getName().compareTo(b.getName());
        });
        JSONArray out = new JSONArray();
        for (Voice v : list) {
            try {
                out.put(new JSONObject()
                        .put("name", v.getName())
                        .put("lang", v.getLocale().toLanguageTag())
                        .put("local", !v.isNetworkConnectionRequired())
                        .put("def", v.equals(defaultVoice)));
            } catch (JSONException ignored) {
            }
        }
        emit(new Msg("voices", null).put("voices", out));
    }

    private final class Progress extends UtteranceProgressListener {
        @Override public void onStart(String id) { emit(event("tts.start", id)); }
        @Override public void onDone(String id) { emit(event("tts.end", id)); }
        @Override public void onStop(String id, boolean interrupted) { emit(event("tts.end", id)); }
        @Override public void onError(String id, int errorCode) { emit(event("tts.error", id)); }
        @Override @Deprecated public void onError(String id) { emit(event("tts.error", id)); }
    }

    // ── Messaging ─────────────────────────────────────────────────────────

    /** JSONObject whose put() doesn't throw, for building messages inline. */
    private static final class Msg {
        final JSONObject json = new JSONObject();

        Msg(String type, String id) {
            put("t", type);
            if (id != null) put("id", id);
        }

        Msg put(String key, Object value) {
            try {
                json.put(key, value);
            } catch (JSONException ignored) {
            }
            return this;
        }
    }

    private static Msg event(String type, String id) {
        return new Msg(type, id);
    }

    /** Safe from any thread (TTS callbacks arrive on a binder thread). */
    private void emit(Msg msg) {
        String text = msg.json.toString();
        activity.runOnUiThread(() -> {
            if (page == null) return;
            try {
                page.postMessage(text);
            } catch (RuntimeException ignored) {   // page navigated away
            }
        });
    }

    private static double clamp(double v, double lo, double hi) {
        return Double.isNaN(v) ? 1 : Math.max(lo, Math.min(hi, v));
    }

    void destroy() {
        if (recognitionId != null) endRecognition(recognitionId, "aborted");
        if (tts != null) {
            tts.shutdown();
            tts = null;
        }
        page = null;
    }
}
