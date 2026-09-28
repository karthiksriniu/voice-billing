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
    }

    @JavascriptInterface
    fun handsFree(on: Boolean) {
        Bus.handsFreeWanted = on
        VoiceService.send(ctx, if (on) VoiceService.ACTION_START else VoiceService.ACTION_STOP)
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

    @JavascriptInterface
    fun version(): String = BuildConfig.VERSION_NAME
}
