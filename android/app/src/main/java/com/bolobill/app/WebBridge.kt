package com.bolobill.app

import android.content.Context
import android.content.Intent
import android.provider.Settings
import android.webkit.JavascriptInterface
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

    /** True once the shopkeeper has granted notification access (D5 auto-confirmation). */
    @JavascriptInterface
    fun paymentListenerEnabled(): Boolean = PaymentListener.isEnabled(ctx)

    /** Opens the system screen where it is granted. There is no way to grant it in-app. */
    @JavascriptInterface
    fun openPaymentListenerSettings() {
        ctx.startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    }

    @JavascriptInterface
    fun version(): String = BuildConfig.VERSION_NAME
}
