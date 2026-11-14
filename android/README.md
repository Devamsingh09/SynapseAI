# SynapseAI for Android

A native shell around the live site (https://synapse-ai-orcin.vercel.app), so
the app has exactly the mobile web UI and picks up every web deploy without a
new APK.

**Download:** https://github.com/Devamsingh09/SynapseAI/releases/download/android-latest/SynapseAI.apk

## What the app adds

- Full-screen, edge-to-edge WebView; status bar in the header color; the page
  resizes above the keyboard.
- Voice mode and Read aloud: Android's WebView has no Web Speech API, so
  `assets/bridge.js` provides `SpeechRecognition` and `speechSynthesis` on the
  SynapseAI origin, backed by the phone's recognizer and TTS (`NativeVoice.java`).
- File upload picker (PDF, DOCX, images), external links open in the browser,
  offline screen with Try again, back button closes the drawer, splash screen.
- App Links: `public/.well-known/assetlinks.json` lets verify/reset emails open
  in the app.

## Building

GitHub Actions (`.github/workflows/android.yml`) builds on every push that
touches `android/` and replaces the `android-latest` release. Run it by hand
from the Actions tab → Android APK → Run workflow.

Signing uses the repository secrets `SIGNING_KEYSTORE_BASE64` and
`SIGNING_PASSWORD`. Keep a backup of the keystore: without the same key, a new
APK won't install over the old one.

Locally (needs the Android SDK): put a `keystore.properties` next to
`settings.gradle` (`storeFile`, `storePassword`, `keyAlias`, `keyPassword`) and
run `gradle assembleRelease`.
