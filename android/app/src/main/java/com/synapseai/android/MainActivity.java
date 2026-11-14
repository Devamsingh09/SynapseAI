package com.synapseai.android;

import android.Manifest;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.os.SystemClock;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.MimeTypeMap;
import android.webkit.PermissionRequest;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import androidx.activity.ComponentActivity;
import androidx.activity.EdgeToEdge;
import androidx.activity.OnBackPressedCallback;
import androidx.activity.SystemBarStyle;
import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.core.content.ContextCompat;
import androidx.core.graphics.Insets;
import androidx.core.splashscreen.SplashScreen;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;
import androidx.webkit.WebViewCompat;
import androidx.webkit.WebViewFeature;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.function.Consumer;

/**
 * SynapseAI for Android: the live web app (same UI as the mobile site) in a
 * full-screen WebView, plus what a browser would otherwise provide — file
 * upload, voice (native speech recognition and text-to-speech, see
 * {@link NativeVoice}), external links in the browser, an offline screen and
 * the back button.
 */
public class MainActivity extends ComponentActivity {

    private static final Uri APP_URI = Uri.parse(BuildConfig.APP_URL);
    private static final String APP_HOST = APP_URI.getHost();
    private static final String APP_ORIGIN = APP_URI.getScheme() + "://" + APP_HOST;
    private static final long SPLASH_MAX_MS = 6000;

    /** Closes the mobile sidebar drawer if it's open; returns whether it was. */
    private static final String CLOSE_DRAWER_JS =
            "(function(){var o=document.querySelector('.overlay.open');"
            + "if(o){o.click();return true}return false})()";

    private WebView web;
    private View offline;
    private View statusScrim;
    private NativeVoice voice;
    private ValueCallback<Uri[]> fileCallback;
    private Consumer<Boolean> pendingMic;
    private boolean firstLoadDone;
    private boolean mainFrameError;

    private final ActivityResultLauncher<Intent> filePicker = registerForActivityResult(
            new ActivityResultContracts.StartActivityForResult(), result -> {
                if (fileCallback == null) return;
                fileCallback.onReceiveValue(WebChromeClient.FileChooserParams
                        .parseResult(result.getResultCode(), result.getData()));
                fileCallback = null;
            });

    private final ActivityResultLauncher<String> micPermission = registerForActivityResult(
            new ActivityResultContracts.RequestPermission(), granted -> {
                Consumer<Boolean> cb = pendingMic;
                pendingMic = null;
                if (!granted && !shouldShowRequestPermissionRationale(Manifest.permission.RECORD_AUDIO)) {
                    Toast.makeText(this, R.string.mic_denied, Toast.LENGTH_LONG).show();
                }
                if (cb != null) cb.accept(granted);
            });

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        SplashScreen splash = SplashScreen.installSplashScreen(this);
        super.onCreate(savedInstanceState);
        long startedAt = SystemClock.uptimeMillis();
        splash.setKeepOnScreenCondition(() ->
                !firstLoadDone && SystemClock.uptimeMillis() - startedAt < SPLASH_MAX_MS);

        EdgeToEdge.enable(this,
                SystemBarStyle.dark(Color.TRANSPARENT),
                SystemBarStyle.dark(Color.TRANSPARENT));
        setContentView(R.layout.activity_main);

        View root = findViewById(R.id.root);
        statusScrim = findViewById(R.id.status_scrim);
        web = findViewById(R.id.web);
        offline = findViewById(R.id.offline);
        findViewById(R.id.retry).setOnClickListener(v -> web.reload());
        applyInsets(root);

        voice = new NativeVoice(this, this::withMicPermission);
        setUpWebView();

        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
            @Override
            public void handleOnBackPressed() {
                onBack();
            }
        });

        if (savedInstanceState == null || web.restoreState(savedInstanceState) == null) {
            web.loadUrl(urlFrom(getIntent()));
        }
    }

    /** Status bar area gets the header color; the page sits between the system bars and above the keyboard. */
    private void applyInsets(View root) {
        ViewCompat.setOnApplyWindowInsetsListener(root, (v, insets) -> {
            Insets bars = insets.getInsets(WindowInsetsCompat.Type.systemBars()
                    | WindowInsetsCompat.Type.displayCutout());
            Insets ime = insets.getInsets(WindowInsetsCompat.Type.ime());
            v.setPadding(bars.left, 0, bars.right, Math.max(bars.bottom, ime.bottom));
            ViewGroup.LayoutParams lp = statusScrim.getLayoutParams();
            if (lp.height != bars.top) {
                lp.height = bars.top;
                statusScrim.setLayoutParams(lp);
            }
            return WindowInsetsCompat.CONSUMED;
        });
    }

    private void setUpWebView() {
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);   // spoken replies start on their own
        s.setAllowFileAccess(false);
        s.setSupportMultipleWindows(false);              // target=_blank goes through shouldOverrideUrlLoading
        s.setUserAgentString(s.getUserAgentString() + " SynapseAndroid/" + BuildConfig.VERSION_NAME);
        web.setBackgroundColor(ContextCompat.getColor(this, R.color.syn_bg));
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG);
        CookieManager.getInstance().setAcceptCookie(true);

        installVoiceBridge();
        web.setWebViewClient(new Client());
        web.setWebChromeClient(new ChromeClient());
    }

    /**
     * The site uses the browser's speech recognition and speech synthesis,
     * which Android's WebView doesn't have. A small script (assets/bridge.js)
     * provides both on the SynapseAI origin only, backed by {@link NativeVoice}.
     */
    private void installVoiceBridge() {
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)
                || !WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) {
            return;   // very old WebView: everything works except voice
        }
        Set<String> origins = Collections.singleton(APP_ORIGIN);
        WebViewCompat.addWebMessageListener(web, "SynapseNative", origins,
                (view, message, sourceOrigin, isMainFrame, reply) -> {
                    if (isMainFrame) voice.onMessage(message.getData(), reply);
                });
        String flags = "window.__SYNAPSE_NATIVE__={sr:" + NativeVoice.recognitionAvailable(this) + "};\n";
        WebViewCompat.addDocumentStartJavaScript(web, flags + readAsset("bridge.js"), origins);
    }

    private final class Client extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            Uri uri = request.getUrl();
            if (isAppUrl(uri)) return false;
            openExternally(uri);
            return true;
        }

        @Override
        public void onPageStarted(WebView view, String url, Bitmap favicon) {
            mainFrameError = false;
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            firstLoadDone = true;
            if (!mainFrameError) offline.setVisibility(View.GONE);
            CookieManager.getInstance().flush();
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
            if (request.isForMainFrame()) {
                mainFrameError = true;
                offline.setVisibility(View.VISIBLE);
            }
        }

        @Override
        public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
            // The page's renderer crashed or was killed for memory: start fresh.
            ((ViewGroup) view.getParent()).removeView(view);
            view.destroy();
            web = null;
            recreate();
            return true;
        }
    }

    private final class ChromeClient extends WebChromeClient {
        @Override
        public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
            if (fileCallback != null) fileCallback.onReceiveValue(null);
            fileCallback = callback;
            try {
                filePicker.launch(pickerIntent(params));
                return true;
            } catch (ActivityNotFoundException e) {
                fileCallback = null;
                Toast.makeText(MainActivity.this, R.string.no_file_picker, Toast.LENGTH_SHORT).show();
                return false;
            }
        }

        @Override
        public void onPermissionRequest(PermissionRequest request) {
            Uri origin = request.getOrigin();
            boolean ours = origin != null && APP_HOST.equalsIgnoreCase(origin.getHost());
            boolean audio = Arrays.asList(request.getResources())
                    .contains(PermissionRequest.RESOURCE_AUDIO_CAPTURE);
            if (!ours || !audio) {
                request.deny();
                return;
            }
            withMicPermission(granted -> {
                if (granted) request.grant(new String[]{PermissionRequest.RESOURCE_AUDIO_CAPTURE});
                else request.deny();
            });
        }
    }

    /** Opens the system file picker limited to what the page's input accepts (".pdf,.docx,image/*"). */
    private static Intent pickerIntent(WebChromeClient.FileChooserParams params) {
        Intent intent = new Intent(Intent.ACTION_GET_CONTENT)
                .addCategory(Intent.CATEGORY_OPENABLE)
                .setType("*/*");
        List<String> mimes = new ArrayList<>();
        for (String raw : params.getAcceptTypes()) {
            if (raw == null) continue;
            for (String part : raw.split(",")) {
                String type = part.trim().toLowerCase(Locale.ROOT);
                if (type.isEmpty()) continue;
                if (type.startsWith(".")) {
                    String ext = type.substring(1);
                    String mime = MimeTypeMap.getSingleton().getMimeTypeFromExtension(ext);
                    if (mime == null && ext.equals("docx")) {
                        mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
                    }
                    if (mime != null) mimes.add(mime);
                } else {
                    mimes.add(type);
                }
            }
        }
        if (!mimes.isEmpty()) intent.putExtra(Intent.EXTRA_MIME_TYPES, mimes.toArray(new String[0]));
        if (params.getMode() == WebChromeClient.FileChooserParams.MODE_OPEN_MULTIPLE) {
            intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
        }
        return intent;
    }

    /** Runs {@code then} with whether the microphone may be used, asking the first time. */
    void withMicPermission(Consumer<Boolean> then) {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
                == PackageManager.PERMISSION_GRANTED) {
            then.accept(true);
            return;
        }
        Consumer<Boolean> earlier = pendingMic;
        pendingMic = earlier == null ? then : granted -> {
            earlier.accept(granted);
            then.accept(granted);
        };
        if (earlier == null) micPermission.launch(Manifest.permission.RECORD_AUDIO);
    }

    /** Back: close the sidebar drawer → go back between login/signup pages → leave the app (keeping its state). */
    private void onBack() {
        if (web == null || offline.getVisibility() == View.VISIBLE) {
            moveTaskToBack(true);
            return;
        }
        web.evaluateJavascript(CLOSE_DRAWER_JS, closed -> {
            if ("true".equals(closed)) return;
            String path = Uri.parse(String.valueOf(web.getUrl())).getPath();
            boolean onMainScreen = path == null || path.isEmpty() || path.equals("/");
            if (!onMainScreen && web.canGoBack()) web.goBack();
            else moveTaskToBack(true);
        });
    }

    private static boolean isAppUrl(Uri uri) {
        String scheme = uri.getScheme();
        if (scheme == null) return true;
        switch (scheme) {
            case "about":
            case "blob":
            case "data":
            case "javascript":
                return true;
            case "https":
                return APP_HOST.equalsIgnoreCase(uri.getHost());
            default:
                return false;
        }
    }

    private void openExternally(Uri uri) {
        Intent intent = new Intent(Intent.ACTION_VIEW, uri);
        String scheme = uri.getScheme();
        if ("http".equals(scheme) || "https".equals(scheme)) intent.addCategory(Intent.CATEGORY_BROWSABLE);
        try {
            startActivity(intent);
        } catch (ActivityNotFoundException e) {
            Toast.makeText(this, R.string.no_app_for_link, Toast.LENGTH_SHORT).show();
        }
    }

    private static String urlFrom(Intent intent) {
        Uri data = intent == null ? null : intent.getData();
        return data != null && isAppUrl(data) && "https".equals(data.getScheme())
                ? data.toString()
                : BuildConfig.APP_URL;
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        if (web != null && intent.getData() != null) web.loadUrl(urlFrom(intent));
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        if (web != null) web.saveState(outState);
    }

    @Override
    protected void onPause() {
        super.onPause();
        CookieManager.getInstance().flush();   // keep the login across app restarts
    }

    @Override
    protected void onDestroy() {
        if (voice != null) voice.destroy();
        if (web != null) {
            web.destroy();
            web = null;
        }
        super.onDestroy();
    }

    private String readAsset(String name) {
        try (InputStream in = getAssets().open(name)) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            for (int n; (n = in.read(buf)) > 0; ) out.write(buf, 0, n);
            return out.toString(StandardCharsets.UTF_8.name());
        } catch (IOException e) {
            throw new IllegalStateException("missing asset " + name, e);
        }
    }
}
