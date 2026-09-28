package com.synthia.app

import android.Manifest
import android.annotation.SuppressLint
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.util.Log
import android.view.KeyEvent
import android.webkit.ConsoleMessage
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.graphics.toColorInt
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import com.synthia.app.databinding.ActivityMainBinding
import org.json.JSONObject

/* The bill, still a web page; the microphone, no longer.
 *
 * The UI is served rather than bundled deliberately. A sideloaded APK has no update
 * channel, so every fix that can land without a reinstall should — and the parser, the
 * catalog and the whole billing surface can. What the APK exists to carry is the part a
 * browser cannot do: an always-on keyword model that never leaves the phone, a microphone
 * that outlives the screen, and payment confirmation read from the notification shade.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var ui: ActivityMainBinding

    /* Granting the microphone does not by itself turn hands-free on — the page owns that
     * switch, and the phone should not start listening because a permission dialog was
     * answered. What this does is un-stick the case where the page asked for hands-free
     * before the permission existed: the service refused, and nothing would have asked
     * again. */
    private val askAudio = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()) { granted ->
        if (granted[Manifest.permission.RECORD_AUDIO] == true && Bus.handsFreeWanted) {
            VoiceService.send(this, VoiceService.ACTION_START)
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Draw behind the system bars, then pad the frame back out of their way. On a
        // bezel-less phone (and on anything targeting API 35, where edge-to-edge stopped
        // being optional) the page otherwise renders underneath the clock and the gesture
        // pill, and the shop's own header is the part that gets covered.
        WindowCompat.setDecorFitsSystemWindows(window, false)
        ui = ActivityMainBinding.inflate(layoutInflater)
        setContentView(ui.root)
        applyInsets()

        with(ui.web.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            // The page no longer asks for a microphone, so nothing here needs to grant one.
        }
        /* The page's console into logcat. On a sideloaded pilot there is no devtools and
         * no way to ask a shopkeeper what the browser said, so this is the only channel
         * between a page-side failure and anybody who can fix it. */
        ui.web.webChromeClient = object : WebChromeClient() {
            override fun onConsoleMessage(m: ConsoleMessage): Boolean {
                Log.i("SynthiaWeb", "${m.message()}  [${m.sourceId()}:${m.lineNumber()}]")
                return true
            }
        }
        ui.web.addJavascriptInterface(WebBridge(this), "Bolo")
        ui.web.webViewClient = object : WebViewClient() {
            override fun onPageFinished(view: WebView?, url: String?) {
                // native.js reads this to know it is not in a browser. Injecting it here
                // rather than shipping it in the page means an older deployed build still
                // works with a newer APK.
                view?.evaluateJavascript("window.BOLO_NATIVE = true;", null)
            }
        }

        Bus.toWeb = { event, payload -> deliver(event, payload) }
        ui.web.loadUrl(BuildConfig.WEB_BASE)

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (ui.web.canGoBack()) ui.web.goBack() else finish()
            }
        })

        // Restore the microphone the shopkeeper left on, before the page has had a chance
        // to ask for it.
        Bus.handsFreeWanted = VoiceService.handsFreeEnabled(this)
        requestPermissions()
        if (Bus.handsFreeWanted &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            == PackageManager.PERMISSION_GRANTED) {
            VoiceService.send(this, VoiceService.ACTION_START)
        }
    }

    /* The page already knows how to do this, and doing it twice is what put a band above
     * the header.
     *
     * index.html sets viewport-fit=cover and style.css pads `.bar` by
     * env(safe-area-inset-top) using the header's OWN background — a real bleed, where the
     * status bar sits on the header rather than above it. Padding the frame natively on top
     * of that inset the content twice and left the page background showing through in
     * between. So the frame is not padded at all any more, and the WebView runs edge to
     * edge as the CSS assumes.
     *
     * The keyboard is the one inset the page cannot see. decorFitsSystemWindows(false)
     * stops adjustResize from firing, so the IME — and nothing else — is applied here.
     * Without it the customer's own number field sits under the keyboard they are typing on.
     */
    private fun applyInsets() {
        ViewCompat.setOnApplyWindowInsetsListener(ui.root) { view, insets ->
            val ime = insets.getInsets(WindowInsetsCompat.Type.ime())
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            view.setPadding(0, 0, 0, (ime.bottom - bars.bottom).coerceAtLeast(0))
            insets
        }
    }

    private fun requestPermissions() {
        val need = mutableListOf<String>()
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED) need += Manifest.permission.RECORD_AUDIO
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED) need += Manifest.permission.POST_NOTIFICATIONS
        // Payment confirmation. Declined is survivable — the shopkeeper then confirms by
        // hand, exactly as he does today with a sticker QR.
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECEIVE_SMS)
            != PackageManager.PERMISSION_GRANTED) need += Manifest.permission.RECEIVE_SMS
        if (need.isNotEmpty()) askAudio.launch(need.toTypedArray())
    }

    private fun deliver(event: String, payload: JSONObject) {
        val js = "window.BoloNative && window.BoloNative.on(" +
            "${JSONObject.quote(event)}, ${payload})"
        ui.web.evaluateJavascript(js, null)
    }

    /* Volume-down as a hardware push-to-talk — D2's second escape hatch, and the one a
     * browser could never have. The phone lies on the counter and is pressed without being
     * looked at. */
    override fun onKeyDown(keyCode: Int, event: KeyEvent?): Boolean {
        if (keyCode == KeyEvent.KEYCODE_VOLUME_DOWN) {
            VoiceService.send(this, VoiceService.ACTION_PTT_DOWN); return true
        }
        return super.onKeyDown(keyCode, event)
    }

    override fun onKeyUp(keyCode: Int, event: KeyEvent?): Boolean {
        if (keyCode == KeyEvent.KEYCODE_VOLUME_DOWN) {
            VoiceService.send(this, VoiceService.ACTION_PTT_UP); return true
        }
        return super.onKeyUp(keyCode, event)
    }

    /* The microphone follows the window.
     *
     * The permission the shopkeeper grants says "while using the app", and the honest
     * reading of that is the literal one: nothing listens once he has switched away. A
     * foreground service is still what holds the mic — it has to be, or Android silences
     * the stream the moment the Activity stops being visible — but the service is told to
     * stand down here rather than being left running behind whatever he opened next.
     *
     * The cost is real and accepted: a bill cannot be dictated while he is in his UPI app
     * checking a payment. The alternative is a shop phone that listens to a room full of
     * strangers with the screen off, which is not a thing to ship on a permission grant
     * this vague.
     */
    override fun onResume() {
        super.onResume()
        // The persisted switch, not the in-memory mirror. Bus.handsFreeWanted is set when
        // the page toggles it or when this Activity is first created, and drifts the moment
        // anything else turns hands-free on — which left the microphone off for good after
        // the first time the shopkeeper switched away and came back.
        Bus.handsFreeWanted = VoiceService.handsFreeEnabled(this)
        Bus.appInForeground = true
        applyScreenPolicy()
        // Starting a foreground service is only legal from the foreground, which is
        // exactly where we are. Resuming needs no message at all — the loop reads the flag.
        if (Bus.handsFreeWanted && !VoiceService.running &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            == PackageManager.PERMISSION_GRANTED) {
            VoiceService.send(this, VoiceService.ACTION_START)
        }
    }

    /* Hands-free keeps the screen awake, and that is a deployment decision rather than a
     * convenience.
     *
     * The microphone stops when this Activity pauses — the permission says "while using the
     * app" and onPause is the literal reading of it (see README). But the phone is meant to
     * sit on the counter dictating a bill, and a screen that sleeps after thirty seconds
     * pauses the Activity and takes the microphone with it, mid-order.
     *
     * The alternative was to drop the foreground gate and listen while locked. This is the
     * smaller change and it keeps that promise intact: the screen stays lit only while
     * hands-free is explicitly on, and the customer can read the bill off it while he pays,
     * which is the other half of why the screen wants to be awake anyway.
     *
     * The cost is battery, and it is real on the phone this is aimed at. Accepted for the
     * pilot, deliberately, and it is the first thing to revisit when the trial is over. */
    fun applyScreenPolicy() {
        if (VoiceService.handsFreeEnabled(this)) {
            window.addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        } else {
            window.clearFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        }
    }

    override fun onPause() {
        super.onPause()
        // A flag, not an Intent. See Bus.appInForeground for what the Intent cost us.
        Bus.appInForeground = false
    }

    override fun onDestroy() {
        Bus.toWeb = null
        ui.web.destroy()
        super.onDestroy()
    }
}
