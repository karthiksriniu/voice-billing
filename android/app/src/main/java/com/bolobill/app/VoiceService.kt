package com.bolobill.app

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.ToneGenerator
import android.media.AudioManager
import android.os.Build
import android.os.IBinder
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import com.k2fsa.sherpa.onnx.SileroVadModelConfig
import com.k2fsa.sherpa.onnx.Vad
import com.k2fsa.sherpa.onnx.VadModelConfig
import java.util.concurrent.Executors
import kotlin.concurrent.thread

/* The microphone, and the decision about when the shopkeeper has finished a sentence.
 *
 * This is wake.js's state machine moved out of the browser, with the doorbell replaced by
 * a model that runs on the phone. The move buys four things the page could not have: the
 * mic survives the screen going dark, the wake word never leaves the device, nothing
 * competes for the audio input, and the whole thing keeps working while the WebView is
 * paged out behind a UPI app.
 *
 * The timings below are NOT new. They were paid for with a recording from a real counter
 * and the reasoning is in poc/public/wake.js — the short version is that the pause inside
 * one order (1090ms) is longer than the gap before the room starts up again (520ms), so no
 * endpoint threshold can be both safe and quick. The resolution is to endpoint fast and
 * make being wrong cheap: a clip that cuts mid-order is picked straight back up by the
 * continuation window without the name being said again, and both halves bill to the same
 * bill.
 */
class VoiceService : Service() {

    private enum class State { IDLE, LISTENING, CAPTURING, HOLDING }

    @Volatile private var alive = true
    @Volatile private var handsFree = false
    @Volatile private var ptt = false
    @Volatile private var state = State.IDLE

    private lateinit var kws: Kws
    private lateinit var vad: Vad
    private lateinit var transcriber: Transcriber

    private var record: AudioRecord? = null
    private var worker: Thread? = null

    // Single thread on purpose: two chained clips must reach the bill in the order they
    // were spoken, and a pool would race them.
    private val uploads = Executors.newSingleThreadExecutor()

    private val clip = ArrayList<Short>(SAMPLE_RATE * 13)
    private var clipStartedAt = 0L
    private var lastSpeechAt = 0L
    private var heardSpeech = false
    private var holdStartedAt = 0L
    private var chain = 0

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        transcriber = Transcriber(BuildConfig.WEB_BASE)
        startForeground(NOTIF_ID, notification(getString(R.string.notif_listening)))
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        Log.i(TAG, "action=${intent?.action}")
        when (intent?.action) {
            ACTION_START -> { handsFree = true; remember(true); ensureWorker() }
            ACTION_STOP -> { handsFree = false; ptt = false; remember(false) }
            ACTION_PTT_DOWN -> { ptt = true; ensureWorker() }
            ACTION_PTT_UP -> ptt = false
            ACTION_SHUTDOWN -> { stopSelf(); return START_NOT_STICKY }
        }
        return START_STICKY
    }

    /* The switch lives here, not in the page's localStorage.
     *
     * A shop's phone reboots, runs out of battery, gets cleared by a task killer. The
     * microphone has to come back on its own terms — before the WebView has loaded, and
     * whether or not the page ever loads at all. Storing it in the browser made the
     * service's own state a fact only the browser knew. */
    private fun remember(on: Boolean) =
        getSharedPreferences(PREFS, MODE_PRIVATE).edit().putBoolean(KEY_HANDS_FREE, on).apply()

    private fun ensureWorker() {
        if (worker != null) return
        if (ContextCompat.checkSelfPermission(this, android.Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED) {
            Bus.emit("voice_error", "reason" to "no_permission")
            return
        }
        worker = thread(name = "voice", priority = Thread.MAX_PRIORITY) { loop() }
    }

    /* ---- the loop ---- */

    private fun loop() {
        try {
            kws = Kws(assets)
            vad = Vad(assetManager = assets, config = vadConfig())
        } catch (e: Throwable) {
            Log.e(TAG, "model load failed", e)
            Bus.emit("voice_error", "reason" to "model_load", "detail" to (e.message ?: ""))
            return
        }
        Bus.emit("voice_ready")

        val window = ShortArray(WINDOW)
        val floats = FloatArray(WINDOW)

        while (alive) {
            // Nothing holds the microphone while nobody has asked us to listen. The whole
            // consent story rests on this being literally true.
            if (!handsFree && !ptt) {
                closeMic()
                if (state != State.IDLE) { state = State.IDLE; note(R.string.notif_listening) }
                Thread.sleep(60)
                continue
            }
            if (!openMic()) { Thread.sleep(250); continue }

            val n = record?.read(window, 0, WINDOW) ?: 0
            if (n <= 0) { Thread.sleep(5); continue }
            for (i in 0 until n) floats[i] = window[i] / 32768f

            val now = System.currentTimeMillis()

            if (DEBUG_AUDIO) {
                frames++
                var peak = 0
                for (i in 0 until n) { val a = kotlin.math.abs(window[i].toInt()); if (a > peak) peak = a }
                if (peak > loudest) loudest = peak
                if (now - lastBeat > 2000) {
                    Log.i(TAG, "beat state=$state frames=$frames peak=$loudest chain=$chain")
                    lastBeat = now; frames = 0; loudest = 0
                }
            }

            // Push-to-talk overrides whatever the doorbell was doing.
            if (ptt && state != State.CAPTURING) beginCapture(fromWake = false)
            if (!ptt && state == State.CAPTURING && pttWasTheTrigger) { endCapture(final = true); continue }

            when (state) {
                State.IDLE, State.LISTENING -> {
                    if (kws.accept(floats.copyOf(n)) != null) beginCapture(fromWake = true)
                }

                State.CAPTURING -> {
                    for (i in 0 until n) clip.add(window[i])
                    vad.acceptWaveform(floats.copyOf(n))
                    if (vad.isSpeechDetected()) { lastSpeechAt = now; heardSpeech = true }

                    val clipMs = now - clipStartedAt
                    when {
                        // He said the name and then nothing. Don't send the room.
                        !heardSpeech && clipMs > NO_SPEECH_MS -> abandonCapture()
                        heardSpeech && now - lastSpeechAt > SILENCE_MS -> endCapture(final = false)
                        clipMs > MAX_CLIP_MS -> endCapture(final = false)
                        else -> {}
                    }
                }

                State.HOLDING -> {
                    vad.acceptWaveform(floats.copyOf(n))
                    if (vad.isSpeechDetected() && chain < MAX_CHAIN) {
                        resumeCapture()
                        // The first window of the continuation is speech; keep it.
                        for (i in 0 until n) clip.add(window[i])
                    } else if (now - holdStartedAt > CONTINUE_MS) {
                        standDown()
                    }
                }
            }
        }
        closeMic()
    }

    /* ---- transitions ---- */

    private var pttWasTheTrigger = false
    private var frames = 0
    private var loudest = 0
    private var lastBeat = 0L

    private fun beginCapture(fromWake: Boolean) {
        state = State.CAPTURING
        pttWasTheTrigger = !fromWake
        chain = 1
        clip.clear()
        clipStartedAt = System.currentTimeMillis()
        lastSpeechAt = clipStartedAt
        heardSpeech = false
        vad.reset()
        // The doorbell stops ringing before the order is read, so the name can never end
        // up inside the clip and be billed as an item.
        kws.reset()
        feedback(880, 90)
        note(R.string.notif_recording)
        Bus.emit("capture", "state" to "start", "wake" to fromWake)
    }

    private fun resumeCapture() {
        state = State.CAPTURING
        chain++
        clip.clear()
        clipStartedAt = System.currentTimeMillis()
        lastSpeechAt = clipStartedAt
        heardSpeech = true
        Bus.emit("capture", "state" to "resume", "chain" to chain)
    }

    private fun endCapture(final: Boolean) {
        val ms = System.currentTimeMillis() - clipStartedAt
        val pcm = clip.toShortArray()
        clip.clear()

        Log.i(TAG, "endCapture ms=$ms samples=${pcm.size} heardSpeech=$heardSpeech final=$final chain=$chain")
        if (ms >= MIN_CLIP_MS && heardSpeech) dispatch(pcm, ms)

        if (!final && !pttWasTheTrigger && handsFree && chain < MAX_CHAIN) {
            state = State.HOLDING
            holdStartedAt = System.currentTimeMillis()
            vad.reset()
            Bus.emit("capture", "state" to "hold")
        } else {
            standDown()
        }
    }

    private fun abandonCapture() {
        clip.clear()
        Bus.emit("capture", "state" to "empty")
        standDown()
    }

    private fun standDown() {
        chain = 0
        pttWasTheTrigger = false
        vad.reset()
        kws.reset()
        state = if (handsFree) State.LISTENING else State.IDLE
        feedback(520, 70)
        note(R.string.notif_listening)
        Bus.emit("capture", "state" to "idle")
    }

    private fun dispatch(pcm: ShortArray, ms: Long) {
        Bus.emit("working")
        uploads.execute {
            Log.i(TAG, "dispatch ${pcm.size} samples -> ${BuildConfig.WEB_BASE}")
            val wav = Wav.encode(pcm)
            val res = transcriber.send(wav, ms)
            Log.i(TAG, "transcribe done ms=${res.ms} err=${res.error}")
            if (res.json != null) {
                Bus.emit("result", res.json.put("native_ms", res.ms).put("clip_ms", ms))
            } else {
                Bus.emit("voice_error", "reason" to (res.error ?: "unknown"))
            }
        }
    }

    /* ---- mic ---- */

    private fun openMic(): Boolean {
        record?.let { return it.recordingState == AudioRecord.RECORDSTATE_RECORDING }
        return try {
            val min = AudioRecord.getMinBufferSize(SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
            val r = AudioRecord(
                // VOICE_RECOGNITION, not MIC: it is the one source whose vendor processing
                // is tuned for speech rather than for a video's soundtrack, and on a cheap
                // handset in a loud room that difference is not subtle.
                MediaRecorder.AudioSource.VOICE_RECOGNITION,
                SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                maxOf(min, WINDOW * 8))
            if (r.state != AudioRecord.STATE_INITIALIZED) { r.release(); return false }
            r.startRecording()
            record = r
            if (state == State.IDLE) state = State.LISTENING
            true
        } catch (e: SecurityException) {
            Bus.emit("voice_error", "reason" to "no_permission"); false
        }
    }

    private fun closeMic() {
        record?.let { try { it.stop() } catch (_: Exception) {}; it.release() }
        record = null
    }

    /* ---- feedback ---- */

    private var tones: ToneGenerator? = null

    private fun feedback(hz: Int, ms: Int) {
        // A courtesy, never the mechanism — same rule as the browser build.
        try {
            if (tones == null) tones = ToneGenerator(AudioManager.STREAM_NOTIFICATION, 70)
            tones?.startTone(if (hz > 700) ToneGenerator.TONE_PROP_BEEP
                             else ToneGenerator.TONE_PROP_ACK, ms)
        } catch (_: Exception) {}
        try {
            val v = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S)
                getSystemService(VibratorManager::class.java).defaultVibrator
            else @Suppress("DEPRECATION") getSystemService(Vibrator::class.java)
            v?.vibrate(VibrationEffect.createOneShot(20, VibrationEffect.DEFAULT_AMPLITUDE))
        } catch (_: Exception) {}
    }

    /* ---- notification ---- */

    private fun note(textRes: Int) {
        getSystemService(android.app.NotificationManager::class.java)
            .notify(NOTIF_ID, notification(getString(textRes)))
    }

    private fun notification(text: String): Notification {
        val open = PendingIntent.getActivity(this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val stop = PendingIntent.getService(this, 1,
            Intent(this, VoiceService::class.java).setAction(ACTION_SHUTDOWN),
            PendingIntent.FLAG_IMMUTABLE)
        return NotificationCompat.Builder(this, App.CHANNEL_VOICE)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_mic)
            .setContentIntent(open)
            .addAction(0, getString(R.string.stop), stop)
            .setOngoing(true)
            .setSilent(true)
            .build()
    }

    private fun vadConfig() = VadModelConfig(
        sileroVadModelConfig = SileroVadModelConfig(
            model = "kws/silero_vad.onnx",
            threshold = 0.5f,
            // These two are Silero's own segment shaping, deliberately looser than our
            // endpointer: the decision about when a sentence has ended is made above, from
            // SILENCE_MS, so the model is only being asked "is this speech right now".
            minSilenceDuration = 0.10f,
            minSpeechDuration = 0.10f,
            windowSize = WINDOW,
            maxSpeechDuration = 20f,
        ),
        sampleRate = SAMPLE_RATE,
        numThreads = 1,
        provider = "cpu",
    )

    override fun onDestroy() {
        alive = false
        worker?.join(1000)
        closeMic()
        uploads.shutdown()
        tones?.release()
        if (this::kws.isInitialized) kws.release()
        if (this::vad.isInitialized) vad.release()
        super.onDestroy()
    }

    companion object {
        private const val TAG = "VoiceService"
        private const val NOTIF_ID = 42
        private const val SAMPLE_RATE = Kws.SAMPLE_RATE

        /** 512 samples = 32 ms, which is the window Silero v5 requires and a fine
         *  granularity for the keyword decoder. Everything downstream is a multiple of it. */
        private const val WINDOW = 512

        // Ported verbatim from wake.js. Do not re-derive these from first principles;
        // they came off a real counter recording. See the comment block at the top.
        private const val SILENCE_MS = 800L
        private const val CONTINUE_MS = 4000L
        private const val MAX_CHAIN = 3
        private const val MAX_CLIP_MS = 12000L
        private const val MIN_CLIP_MS = 400L
        private const val NO_SPEECH_MS = 3000L

        /** Heartbeat into logcat. The wake word cannot be tuned from a device you cannot
         *  see into, and "nothing happened" has too many causes to guess between. */
        private val DEBUG_AUDIO = BuildConfig.DEBUG

        private const val PREFS = "voice"
        private const val KEY_HANDS_FREE = "hands_free"

        fun handsFreeEnabled(ctx: Context): Boolean =
            ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getBoolean(KEY_HANDS_FREE, false)

        const val ACTION_START = "start"
        const val ACTION_STOP = "stop"
        const val ACTION_PTT_DOWN = "ptt_down"
        const val ACTION_PTT_UP = "ptt_up"
        const val ACTION_SHUTDOWN = "shutdown"

        fun send(ctx: Context, action: String) {
            val i = Intent(ctx, VoiceService::class.java).setAction(action)
            ContextCompat.startForegroundService(ctx, i)
        }
    }
}
