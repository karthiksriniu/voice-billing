package com.synthia.app

import android.os.Handler
import android.os.Looper
import org.json.JSONObject

/* The only channel between the microphone (a service) and the bill (a web page).
 *
 * Both live in one process, so this is a pair of volatile fields rather than a broadcast.
 * The service must never hold a reference to the Activity: the microphone outlives the
 * window by design — that is the whole point of moving it out of the browser — and a
 * service that keeps a dead WebView alive is the classic way to leak one. */
object Bus {

    /** Set by MainActivity while its WebView is alive, cleared when it is not. */
    @Volatile var toWeb: ((event: String, payload: JSONObject) -> Unit)? = null

    /** What the page last told us about itself. The service needs it to post a clip. */
    @Volatile var shopId: String = ""
    @Volatile var mode: String = "sell"
    /* English until the page says otherwise.
     *
     * This defaulted to Tamil, which meant a clip recorded before the page had finished
     * loading — or by a shop whose language was never set — was transcribed as ta-IN and
     * came back in Tamil script against a Latin catalog, matching nothing. English is both
     * the safer default and the only one the wake word has ever been measured in. */
    @Volatile var lang: String = "en"

    /** Which physical microphone the service is actually recording from. Rides with every
     *  clip so the measurement log can tell three microphones apart. */
    @Volatile var micLabel: String = "builtin"

    /** What to call him. Empty until the page has loaded a shop, and answering "Yes" on its
     *  own is fine — better than answering to a name that is not his. */
    @Volatile var ownerName: String = ""

    /* The two lines the SERVICE says, supplied by the page rather than by strings.xml.
     *
     * Everything else spoken comes from i18n.js, which already carries six languages and is
     * where a translation gets fixed. These two could not: they are said from the audio loop,
     * one of them before the microphone opens. Rather than keep a second, smaller translation
     * table in Android resources and have Tamil live in two places that drift, the page hands
     * the finished sentence over. The resources remain as the fallback for the moments before
     * any page has loaded. */
    @Volatile var ackLine: String = ""
    @Volatile var notHeardLine: String = ""

    /** Whether the page has asked for hands-free. Survives a permission prompt. */
    @Volatile var handsFreeWanted: Boolean = false

    /* Is the app actually in front of the shopkeeper?
     *
     * This is a plain field and not an Intent, and that is the entire point. It used to be
     * ACTION_SUSPEND sent from Activity.onPause via startForegroundService — which is a
     * foreground-service start issued at the exact moment the app stops being foreground.
     * Android 12+ refuses that, startForeground() then threw
     * ForegroundServiceStartNotAllowedException inside onCreate, and the app died every
     * time the shopkeeper switched away. The service and the Activity share a process;
     * they never needed the system to carry a boolean between them. */
    @Volatile var appInForeground: Boolean = false

    private val main = Handler(Looper.getMainLooper())

    fun emit(event: String, payload: JSONObject = JSONObject()) {
        val sink = toWeb ?: return
        main.post { sink(event, payload) }
    }

    fun emit(event: String, vararg pairs: Pair<String, Any?>) =
        emit(event, JSONObject().apply { pairs.forEach { (k, v) -> put(k, v ?: JSONObject.NULL) } })
}
