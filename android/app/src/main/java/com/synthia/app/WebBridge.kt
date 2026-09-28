package com.synthia.app

import android.content.Context
import android.content.pm.PackageManager
import android.webkit.JavascriptInterface
import androidx.core.content.ContextCompat
import org.json.JSONObject

/* What the page may ask the phone to do.
 *
 * Every method here is reachable by any JavaScript running in the WebView, so the surface
 * is kept to things that are harmless if called by something we did not write: start and
 * stop our own microphone, and tell us which shop the page thinks it is billing for.
 * Nothing here reads a file, opens a URL, or returns anything about the device. */
class WebBridge(private val ctx: Context) {

    @JavascriptInterface
    fun setContext(json: String) {
        val o = runCatching { JSONObject(json) }.getOrNull() ?: return
        Bus.shopId = o.optString("shop_id", Bus.shopId)
        Bus.mode = o.optString("mode", Bus.mode)
        Bus.lang = o.optString("lang", Bus.lang)
        Bus.ownerName = o.optString("owner_name", Bus.ownerName)
    }

    @JavascriptInterface
    fun handsFree(on: Boolean) {
        Bus.handsFreeWanted = on
        VoiceService.send(ctx, if (on) VoiceService.ACTION_START else VoiceService.ACTION_STOP)
        /* The screen has to follow the switch, not the next time the Activity happens to
         * resume. Turning hands-free on and watching the display sleep a minute later — taking
         * the microphone with it, because onPause closes it — is the exact failure the
         * keep-awake flag exists to prevent, and it would have survived the whole trial as
         * "it stops after a while". Window flags are main-thread only. */
        (ctx as? MainActivity)?.let { a -> a.runOnUiThread { a.applyScreenPolicy() } }
    }

    /** The authoritative switch state — the service's, not the browser's. */
    @JavascriptInterface
    fun handsFreeOn(): Boolean = VoiceService.handsFreeEnabled(ctx)

    @JavascriptInterface
    fun pttDown() = VoiceService.send(ctx, VoiceService.ACTION_PTT_DOWN)

    @JavascriptInterface
    fun pttUp() = VoiceService.send(ctx, VoiceService.ACTION_PTT_UP)

    /** True once the shopkeeper has granted SMS access, which is what confirms payment. */
    @JavascriptInterface
    fun smsPaymentsEnabled(): Boolean =
        ContextCompat.checkSelfPermission(ctx, android.Manifest.permission.RECEIVE_SMS) ==
            PackageManager.PERMISSION_GRANTED

    /* Which microphone to record from: "auto", "builtin", "wired", "usb" or "bt".
     *
     * A category, never a device list. Enumerating the hardware would tell any script in
     * the WebView what is plugged into this phone, and the trial does not need that to
     * compare three microphones — it needs to be able to pick one and to know afterwards
     * which one actually answered, which rides back on the clip instead. */
    @JavascriptInterface
    fun setMic(pref: String) {
        val clean = when (pref) { "builtin", "wired", "usb", "bt" -> pref; else -> "auto" }
        VoiceService.setMic(ctx, clean)
    }

    @JavascriptInterface
    fun micPref(): String = VoiceService.micPreference(ctx)

    /* Say something out loud, through the phone's voice rather than the WebView's.
     *
     * The page has always had SpeechSynthesis and it works — but it speaks into a microphone
     * that is listening, and the page has no way to shut that microphone. Routed here, the
     * service knows it is talking and goes deaf for the duration, so the bill cannot acquire
     * a line item because the phone read the last one out.
     *
     * Silently does nothing when the service is not running, which is the correct behaviour:
     * if nothing is listening, nothing needs to be talked over, and the page's own fallback
     * is one line away. */
    @JavascriptInterface
    fun say(text: String) {
        VoiceService.speakerRef?.say(text)
    }

    /* Say something and then listen, without the wake word.
     *
     * Only for questions the phone itself asked. Everything else goes through say(), which
     * does not open a microphone — an announcement that started recording would turn every
     * "three items" into an open clip waiting to bill the room. */
    @JavascriptInterface
    fun askThenListen(text: String) {
        if (text.isNotBlank()) VoiceService.ask(ctx, text)
    }

    @JavascriptInterface
    fun version(): String = BuildConfig.VERSION_NAME
}
