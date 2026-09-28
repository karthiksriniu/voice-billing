package com.synthia.app

import android.content.Context
import android.media.MediaPlayer
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

    private val cacheDir = ctx.cacheDir

    private class Job(val text: String, val done: (() -> Unit)?)

    @Volatile private var tts: TextToSpeech? = null
    @Volatile private var ready = false
    @Volatile private var dead = false

    /** Utterances asked for before the engine finished starting. */
    private val pending = ConcurrentLinkedQueue<Job>()

    /** Callbacks keyed by utterance id, so the right one fires when the right line ends. */
    private val callbacks = HashMap<String, () -> Unit>()
    private var seq = 0

    @Volatile private var speakingFlag = false
    @Volatile private var speakingSince = 0L

    /* True between the start of an utterance and its end. The microphone consults this, and
     * goes deaf for exactly as long as it is true — which is why it is read through a
     * deadline rather than straight off the field.
     *
     * The first version of this was a plain boolean, and synthesising the acknowledgement to
     * a file tripped onStart() without ever tripping onDone(), because the completion for a
     * synthesis is handled on a different path. The flag stuck on, the guard swallowed every
     * frame, and the phone sat there with good audio arriving — peak 13249, sixty-three frames
     * every two seconds — and the keyword model simply never saw any of it.
     *
     * That is the second time a latched flag has made this phone permanently deaf, after
     * AudioRecord's recordingState. So the rule now is the same one: nothing that silences the
     * microphone is allowed to do it indefinitely. No utterance runs longer than this; if the
     * flag is still set afterwards it is wrong, and a wrong flag must not cost the shopkeeper
     * his wake word. */
    val speaking: Boolean
        get() {
            if (!speakingFlag) return false
            if (System.currentTimeMillis() - speakingSince > MAX_SPEAK_MS) {
                Log.w(TAG, "speaking flag stuck for ${System.currentTimeMillis() - speakingSince}ms — clearing")
                speakingFlag = false
                return false
            }
            return true
        }

    private fun setSpeaking(on: Boolean) {
        speakingFlag = on
        if (on) speakingSince = System.currentTimeMillis()
    }

    /* The acknowledgement, rendered once and kept.
     *
     * Measured on the pilot phone: speaking "Yes Suresh" live cost 1290ms between the keyword
     * firing and the clip opening, and the first one after launch cost 4043ms while the engine
     * warmed up. That whole time the microphone is deliberately deaf — so a shopkeeper saying
     * "Hey Synthia, two coffee" in one breath, which is the natural way to say it, lost "two
     * coffee" entirely.
     *
     * Only ~350ms of it was the word "Suresh". The rest was synthesising a phrase that never
     * changes and starting the speaker path from cold, on every single wake. So it is
     * synthesised once — when the name is set, long before anybody says the wake word — and
     * played from a prepared MediaPlayer, which is a seek and a start.
     *
     * Everything else the phone says is still spoken live: those lines are different every
     * time, and none of them sits between him and the microphone. */
    private var ackPlayer: MediaPlayer? = null
    private var ackText: String = ""
    @Volatile private var ackReady = false

    init {
        val engine = TextToSpeech(ctx) { status ->
            if (status == TextToSpeech.SUCCESS) {
                tts?.language = Locale("en", "IN")
                ready = true
                Log.i(TAG, "tts ready")
                while (true) (pending.poll() ?: break).let { say(it.text, it.done) }
                // Whatever the name was when the engine was still starting, render it now.
                if (ackText.isNotBlank()) prepareAck(ackText)
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
            override fun onStart(utteranceId: String?) {
                // Rendering to a file makes no sound, so it must not shut the microphone.
                if (utteranceId != null && utteranceId.startsWith(SYNTH)) return
                setSpeaking(true)
            }
            override fun onDone(utteranceId: String?) = finish(utteranceId)
            @Deprecated("required by the base class")
            override fun onError(utteranceId: String?) = finish(utteranceId)
            override fun onError(utteranceId: String?, errorCode: Int) = finish(utteranceId)
        })
        tts = engine
    }

    private fun finish(id: String?) {
        if (id != null && id.startsWith(SYNTH)) { onSynthesised(id); return }
        setSpeaking(false)
        val cb = synchronized(callbacks) { callbacks.remove(id) }
        cb?.invoke()
    }

    /* ---- the pre-rendered acknowledgement ---- */

    /**
     * Render [text] to a file so it can be played back instantly later.
     *
     * Safe to call repeatedly; it does nothing when the phrase has not changed. Called when
     * the shop's name arrives and whenever it is edited, so the cost is paid while nobody is
     * waiting rather than at the moment somebody is.
     */
    fun prepareAck(text: String) {
        if (text == ackText && ackReady) return
        ackText = text
        ackReady = false
        val engine = tts
        if (text.isBlank() || dead || engine == null || !ready) return
        val f = java.io.File(cacheDir, "ack.wav")
        val rc = engine.synthesizeToFile(text, null, f, "$SYNTH${seq++}")
        if (rc != TextToSpeech.SUCCESS) Log.w(TAG, "could not pre-render the acknowledgement")
    }

    private fun onSynthesised(id: String) {
        val f = java.io.File(cacheDir, "ack.wav")
        if (!f.exists() || f.length() == 0L) return
        try {
            ackPlayer?.release()
            val mp = MediaPlayer()
            mp.setDataSource(f.absolutePath)
            mp.prepare()                       // the file is local and already written
            mp.setOnCompletionListener {
                setSpeaking(false)
                val cb = synchronized(callbacks) { callbacks.remove(ACK) }
                cb?.invoke()
            }
            ackPlayer = mp
            ackReady = true
            Log.i(TAG, "acknowledgement pre-rendered (${f.length()} bytes): $ackText")
        } catch (e: Exception) {
            Log.w(TAG, "could not prepare the acknowledgement player: ${e.message}")
            ackReady = false
        }
    }

    /**
     * Play the pre-rendered acknowledgement, then run [done].
     *
     * Falls back to speaking [fallback] live when nothing has been rendered yet — the first
     * wake after an install, or a phone with no TTS data. Slower, but never silent and never
     * stuck: [done] fires either way.
     */
    fun ack(fallback: String, done: () -> Unit) {
        val mp = ackPlayer
        if (!ackReady || mp == null) { say(fallback, done); return }
        try {
            synchronized(callbacks) { callbacks[ACK] = done }
            setSpeaking(true)
            mp.seekTo(0)
            mp.start()
        } catch (e: Exception) {
            Log.w(TAG, "acknowledgement playback failed: ${e.message}")
            synchronized(callbacks) { callbacks.remove(ACK) }
            say(fallback, done)
        }
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
        setSpeaking(true)
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
        setSpeaking(false)
    }

    fun release() {
        try { tts?.stop(); tts?.shutdown() } catch (_: Exception) {}
        try { ackPlayer?.release() } catch (_: Exception) {}
        ackPlayer = null
        ackReady = false
        tts = null
        setSpeaking(false)
    }

    companion object {
        private const val TAG = "Speaker"
        /** Prefix marking a synthesise-to-file completion rather than a spoken one. */
        private const val SYNTH = "synth-"
        /** The one callback key the pre-rendered acknowledgement uses. */
        private const val ACK = "ack"
        /** Longest any single utterance may hold the microphone shut. */
        private const val MAX_SPEAK_MS = 8000L
    }
}
