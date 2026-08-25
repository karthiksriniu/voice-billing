package com.bolobill.app

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
    @Volatile var lang: String = "ta"

    /** Whether the page has asked for hands-free. Survives a permission prompt. */
    @Volatile var handsFreeWanted: Boolean = false

    private val main = Handler(Looper.getMainLooper())

    fun emit(event: String, payload: JSONObject = JSONObject()) {
        val sink = toWeb ?: return
        main.post { sink(event, payload) }
    }

    fun emit(event: String, vararg pairs: Pair<String, Any?>) =
        emit(event, JSONObject().apply { pairs.forEach { (k, v) -> put(k, v ?: JSONObject.NULL) } })
}
