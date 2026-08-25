package com.bolobill.app

import android.Manifest
import android.annotation.SuppressLint
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.view.KeyEvent
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
import com.bolobill.app.databinding.ActivityMainBinding
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
        ui.web.webChromeClient = WebChromeClient()
        ui.web.addJavascriptInterface(WebBridge(this), "Bolo")
        ui.web.webViewClient = object : WebViewClient() {
            override fun onPageFinished(view: WebView?, url: String?) {
                matchBackgroundToPage(view)
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

    /* The frame, not the WebView, carries the padding: the WebView keeps its full height
     * so the page's own background still paints edge to edge underneath, and only the
     * content is inset. The keyboard is folded into the bottom inset because the page has
     * a phone-number field on its first screen, and adjustResize alone does not survive
     * decorFitsSystemWindows = false. */
    private fun applyInsets() {
        ViewCompat.setOnApplyWindowInsetsListener(ui.root) { view, insets ->
            val bars = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
            val ime = insets.getInsets(WindowInsetsCompat.Type.ime())
            view.setPadding(bars.left, bars.top, bars.right, maxOf(bars.bottom, ime.bottom))
            insets
        }
    }

    /* Keeps the padded strips the same colour as the page. The value in colors.xml only has
     * to be right for the first frame; after that the page is the authority, so changing
     * the web theme does not leave a mismatched band under the clock. */
    private fun matchBackgroundToPage(view: WebView?) {
        view?.evaluateJavascript(
            "(document.querySelector('meta[name=theme-color]')||{}).content || ''") { raw ->
            val hex = raw.trim('"', ' ')
            if (hex.startsWith("#")) {
                runCatching { ui.root.setBackgroundColor(hex.toColorInt()) }
            }
        }
    }

    private fun requestPermissions() {
        val need = mutableListOf<String>()
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED) need += Manifest.permission.RECORD_AUDIO
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED) need += Manifest.permission.POST_NOTIFICATIONS
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

    override fun onDestroy() {
        Bus.toWeb = null
        ui.web.destroy()
        super.onDestroy()
    }
}
