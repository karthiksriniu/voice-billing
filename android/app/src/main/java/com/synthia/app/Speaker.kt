package com.synthia.app

import android.content.Context
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import android.util.Log
import java.util.Locale
import java.util.concurrent.ConcurrentLinkedQueue

/* The phone talking back, and — the part that actually matters — knowing when it has stopped.
 *
 * The bill is meant to be dictated with the phone behind the shopkeeper, out of sight. That
 * turns every glance at the screen into something that has to be said out loud, and it turns
 * speech from a courtesy into part of the loop: he says the name, the phone answers, and only
 * then is it his turn again.
 *
 * Which is why this is not just TextToSpeech.speak(). Two things have to be true:
 *
 *   The microphone must not hear the reply. "Yes Suresh" arriving in the capture is an item
 *   on the bill, and the wake word arriving in it wakes the phone again. VoiceService reads
 *   and discards while this is speaking, so the answer is never in the clip — but it can only
 *   do that if it is told exactly when speaking starts and ends.
 *
 *   Capture must begin AFTER the answer, not during it. A shopkeeper starts talking the
 *   instant he hears the acknowledgement; open the clip before that and the first word of
 *   every order is missing.
 *
 * TextToSpeech's own callbacks are delivered on a binder thread, so nothing here touches the
 * audio loop's state directly. The done callback is the only thing that crosses, and the
 * caller turns it into a flag its own thread picks up.
 *
 * Engine init is asynchronous and can fail outright on a phone with no TTS data installed.
 * Speech is a courtesy that became load-bearing, not a mechanism that is allowed to fail
 * closed: anything queued before the engine is ready is spoken when it arrives, and if it
 * never arrives, every callback still fires so the state machine keeps moving. A shopkeeper
 * with a silent phone can still bill; one with a stuck phone cannot.
 */
class Speaker(ctx: Context) {

    private class Job(val text: String, val done: (() -> Unit)?)

    @Volatile private var tts: TextToSpeech? = null
    @Volatile private var ready = false
    @Volatile private var dead = false

    /** Utterances asked for before the engine finished starting. */
    private val pending = ConcurrentLinkedQueue<Job>()

    /** Callbacks keyed by utterance id, so the right one fires when the right line ends. */
    private val callbacks = HashMap<String, () -> Unit>()
    private var seq = 0

    /** True between the start of an utterance and its end. The microphone consults this. */
    @Volatile var speaking = false
        private set

    init {
        val engine = TextToSpeech(ctx) { status ->
            if (status == TextToSpeech.SUCCESS) {
                tts?.language = Locale("en", "IN")
                ready = true
                Log.i(TAG, "tts ready")
                while (true) (pending.poll() ?: break).let { say(it.text, it.done) }
            } else {
                /* No engine. Everything still has to run — the acknowledgement's callback is
                 * what opens the clip, so swallowing it here would leave the phone awake,
                 * silent, and never recording. Fail loud in the log and open on time. */
                dead = true
                Log.w(TAG, "tts unavailable (status=$status) — running silent")
                while (true) (pending.poll() ?: break).let { it.done?.invoke() }
            }
        }
        engine.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(utteranceId: String?) { speaking = true }
            override fun onDone(utteranceId: String?) = finish(utteranceId)
            @Deprecated("required by the base class")
            override fun onError(utteranceId: String?) = finish(utteranceId)
            override fun onError(utteranceId: String?, errorCode: Int) = finish(utteranceId)
        })
        tts = engine
    }

    private fun finish(id: String?) {
        speaking = false
        val cb = synchronized(callbacks) { callbacks.remove(id) }
        cb?.invoke()
    }

    /**
     * Say [text], then run [done].
     *
     * [done] runs on a binder thread and fires exactly once, whether the line was spoken,
     * failed, or never had an engine to speak it.
     */
    fun say(text: String, done: (() -> Unit)? = null) {
        if (text.isBlank()) { done?.invoke(); return }
        if (dead) { done?.invoke(); return }
        val engine = tts
        if (engine == null || !ready) { pending.add(Job(text, done)); return }

        val id = "u${seq++}"
        if (done != null) synchronized(callbacks) { callbacks[id] = done }
        speaking = true
        /* QUEUE_FLUSH, not QUEUE_ADD. On a fast counter the announcements arrive faster than
         * they can be read out, and a queue means he is hearing the bill from thirty seconds
         * ago while adding to the one in front of him. The newest line is the true one; the
         * stale ones are dropped on purpose. */
        val rc = engine.speak(text, TextToSpeech.QUEUE_FLUSH, null, id)
        if (rc != TextToSpeech.SUCCESS) {
            Log.w(TAG, "speak refused rc=$rc")
            finish(id)
        }
    }

    /** Stop mid-sentence — the shopkeeper has moved on and does not need the rest. */
    fun stop() {
        try { tts?.stop() } catch (_: Exception) {}
        speaking = false
    }

    fun release() {
        try { tts?.stop(); tts?.shutdown() } catch (_: Exception) {}
        tts = null
        speaking = false
    }

    companion object { private const val TAG = "Speaker" }
}
