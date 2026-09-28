package com.synthia.app

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.AudioDeviceInfo
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

    private enum class State { IDLE, LISTENING, CAPTURING, HOLDING, SPEAKING }

    @Volatile private var alive = true
    @Volatile private var handsFree = false
    @Volatile private var ptt = false
    @Volatile private var state = State.IDLE
    /** "auto" or one of the kinds in deviceKind(). Which physical microphone to record from. */
    @Volatile private var micPref = "auto"
    /** Set from the main thread, acted on by the worker — see ACTION_SET_MIC. */
    @Volatile private var reopenMic = false

    private lateinit var kws: Kws
    private lateinit var vad: Vad
    private lateinit var transcriber: Transcriber
    private lateinit var speaker: Speaker

    /* Harvest mode. Off in normal use, and nothing about billing changes while it is on
     * except that the doorbell stops answering — the keyword model and the recogniser are the
     * same three files and cannot both own the stream. Toggled over adb rather than from the
     * UI: it is a tuning instrument, not a feature, and the shopkeeper must never find it. */
    @Volatile private var harvesting = false
    private var harvester: Harvester? = null

    /* Asked for on the main thread, acted on by the worker — the loop owns every transition
     * into SPEAKING, and letting onStartCommand make one would race a thread mid-read. */
    @Volatile private var askPending = false
    @Volatile private var askLine = ""

    /* Set from TextToSpeech's binder thread, consumed by the audio loop.
     *
     * A flag rather than a direct call: the callback arrives on somebody else's thread, and
     * beginCapture() clears the clip and resets three models. Doing that underneath a loop
     * that is mid-read is how you get a clip with the last order's tail on the front of it. */
    @Volatile private var speechDone = false
    /** Whether the line now being spoken should be followed by a capture, or by standing down. */
    private var captureAfterSpeech = false
    private var speakingSince = 0L
    /** The name the cached acknowledgement was rendered for. */
    private var lastAckName: String? = null

    private var record: AudioRecord? = null
    private var worker: Thread? = null

    /** The rate the microphone actually runs at. 16 kHz for the handset, 48 kHz for USB. */
    private var micRate = SAMPLE_RATE
    /** Null when the device already gives us SAMPLE_RATE and nothing needs converting. */
    private var resampler: Resampler? = null

    // Single thread on purpose: two chained clips must reach the bill in the order they
    // were spoken, and a pool would race them.
    private val uploads = Executors.newSingleThreadExecutor()

    private val clip = ArrayList<Short>(SAMPLE_RATE * 13)
    private var clipStartedAt = 0L
    private var lastSpeechAt = 0L
    private var heardSpeech = false
    private var holdStartedAt = 0L
    private var chain = 0
    /** Did this clip ever go quiet? A dictated order has pauses in it; a room does not. */
    private var sawGap = false
    /** When the wake that owns the current chain fired. Bounds the whole chain, not a clip. */
    private var wakeStartedAt = 0L

    override fun onBind(intent: Intent?): IBinder? = null

    /* How the harvest is switched on, and why it is a receiver rather than the service's own
     * action: the service is not exported, so `am startservice` is refused, and exporting it
     * to reach a tuning instrument would be a poor trade. A receiver registered at runtime
     * takes the broadcast without widening anything in the manifest.
     *
     * It IS reachable by other apps on the phone, and that is a real if small surface: the
     * worst it can do is stop the doorbell answering until the app is restarted. It reads
     * nothing and writes nothing. Worth it while the wake word is being tuned on a phone in
     * somebody's hand; take it out before this goes anywhere wider than the pilot. */
    private val harvestSwitch = object : android.content.BroadcastReceiver() {
        override fun onReceive(c: Context?, i: Intent?) {
            harvesting = !harvesting
            Log.i(TAG, "harvest mode = $harvesting")
            if (!harvesting) {
                harvester?.let { Log.i(TAG, "HARVEST SUMMARY\n${it.summary()}"); it.release() }
                harvester = null
            }
        }
    }

    override fun onCreate() {
        super.onCreate()
        running = true
        ContextCompat.registerReceiver(this, harvestSwitch,
            android.content.IntentFilter(ACTION_HARVEST), ContextCompat.RECEIVER_EXPORTED)
        micPref = getSharedPreferences(PREFS, MODE_PRIVATE).getString(KEY_MIC, "auto") ?: "auto"
        transcriber = Transcriber(BuildConfig.WEB_BASE)
        /* Never fatal. The platform can refuse this for reasons that are about WHEN it was
         * called rather than anything being wrong — a background start, a missing
         * permission — and an app that dies on the way to the background takes the
         * shopkeeper's open bill with it. Give up the service instead. */
        try {
            startForeground(NOTIF_ID, notification(getString(R.string.notif_listening)))
        } catch (e: Exception) {
            Log.w(TAG, "startForeground refused: ${e.javaClass.simpleName}: ${e.message}")
            Bus.emit("voice_error", "reason" to "foreground_refused")
            stopSelf()
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        Log.i(TAG, "action=${intent?.action}")
        when (intent?.action) {
            ACTION_START -> { handsFree = true; remember(true); ensureWorker() }
            ACTION_STOP -> { handsFree = false; ptt = false; remember(false) }
            ACTION_PTT_DOWN -> { ptt = true; ensureWorker() }
            ACTION_PTT_UP -> ptt = false
            ACTION_SET_MIC -> {
                micPref = intent.getStringExtra(EXTRA_MIC) ?: "auto"
                getSharedPreferences(PREFS, MODE_PRIVATE).edit().putString(KEY_MIC, micPref).apply()
                /* Routing is chosen when the stream is opened, so the running one has to
                 * go — but not from here. onStartCommand runs on the main thread and the
                 * worker owns the AudioRecord; releasing it out from under a read() is a
                 * crash looking for a moment to happen. Ask, and let the loop do it. */
                Log.i(TAG, "mic preference -> $micPref")
                reopenMic = true
            }
            /* The phone asked a question and now has to hear the answer.
             *
             * A confirmation is the one exchange the shopkeeper does not start. He is not
             * going to say the wake word to answer a question he did not ask for, so the
             * capture has to open on its own — after the question has finished being spoken,
             * for the same reason the acknowledgement gates the microphone: "did you say
             * cappuccino" landing in the clip is a bill line, and a yes arriving before the
             * question ends is lost. */
            ACTION_ASK -> {
                val line = intent.getStringExtra(EXTRA_TEXT).orEmpty()
                if (line.isNotBlank() && worker != null) { askLine = line; askPending = true }
            }
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
            // The application context, not this service: TextToSpeech holds what it is given
            // for its lifetime, and the service is the shorter-lived of the two.
            speaker = Speaker(applicationContext)
            speakerRef = speaker
            // Render whatever name we already have before the first wake can arrive.
            refreshAck()
        } catch (e: Throwable) {
            Log.e(TAG, "model load failed", e)
            Bus.emit("voice_error", "reason" to "model_load", "detail" to (e.message ?: ""))
            return
        }
        Bus.emit("voice_ready")

        // Sized for the worst case: a device running at MAX_RATE feeding WINDOW samples out.
        val window = ShortArray(WINDOW * (MAX_RATE / SAMPLE_RATE))
        val native = FloatArray(window.size)
        val floats = FloatArray(WINDOW)

        while (alive) {
            // Nothing holds the microphone while nobody has asked us to listen. The whole
            // consent story rests on this being literally true.
            if ((!handsFree || !Bus.appInForeground) && !ptt) {
                closeMic()
                if (state != State.IDLE) { state = State.IDLE; note(R.string.notif_listening) }
                Thread.sleep(60)
                continue
            }
            if (reopenMic) { reopenMic = false; closeMic() }
            if (!openMic()) { Thread.sleep(250); continue }

            val factor = resampler?.factor ?: 1
            val n = record?.read(window, 0, WINDOW * factor) ?: 0
            /* Negative is an error code, not a short read. ERROR_DEAD_OBJECT (-6) is the
             * one that matters: the record session is gone and every subsequent read
             * returns it too. Lumped in with "no data yet" it span this loop at 5ms with
             * no microphone and no way back — the second half of the same deafness, and
             * a flat battery with it. */
            if (n < 0) {
                Log.w(TAG, "read error $n — reopening mic")
                closeMic()
                Thread.sleep(50)
                continue
            }
            if (n == 0) { Thread.sleep(5); continue }

            /* One conversion, here, before the state machine sees anything.
             *
             * Everything below this line — the keyword model, Silero, the clip that becomes
             * a WAV — assumes SAMPLE_RATE and always has. Rather than teach each of them
             * about a microphone that runs at 48 kHz, the rate is made true again at the
             * point the audio enters the program. `m` is the sample count in 16 kHz terms,
             * and nothing after this cares which microphone produced it. */
            val m: Int
            val rs = resampler
            if (rs == null) {
                for (i in 0 until n) floats[i] = window[i] / 32768f
                m = n
            } else {
                for (i in 0 until n) native[i] = window[i] / 32768f
                m = rs.process(native, n, floats)
            }
            if (m == 0) continue

            val now = System.currentTimeMillis()

            if (DEBUG_AUDIO) {
                frames++
                var peak = 0
                for (i in 0 until m) { val a = kotlin.math.abs((floats[i] * 32768f).toInt()); if (a > peak) peak = a }
                if (peak > loudest) loudest = peak
                if (now - lastBeat > 2000) {
                    Log.i(TAG, "beat state=$state frames=$frames peak=$loudest chain=$chain")
                    lastBeat = now; frames = 0; loudest = 0
                    // Cheap, and this is already the every-two-seconds tick.
                    refreshAck()
                }
            }

            /* The phone is talking, and the microphone can hear it.
             *
             * The frame was read — it has to be, or the buffer overruns and the next second
             * of audio is somebody else's — and then thrown away. Nothing reaches the keyword
             * model, so the phone cannot wake itself on a name it just said, and nothing
             * reaches the clip, so "Yes Suresh" is never an item on the bill.
             *
             * The timeout is not decoration. The whole acknowledgement design makes speech
             * load-bearing: the clip opens when the phone stops talking. An engine that never
             * reports finishing would leave him looking at a phone that answered him once and
             * then listened to nothing for the rest of the day. */
            if (state == State.SPEAKING) {
                if (speechDone || now - speakingSince > SPEAK_TIMEOUT_MS) {
                    if (!speechDone) Log.w(TAG, "tts never reported done — moving on")
                    speechDone = false
                    if (captureAfterSpeech) { captureAfterSpeech = false; beginCapture(fromWake = true) }
                    else standDown()
                }
                continue
            }

            /* A question from the page outranks whatever the doorbell was doing.
             *
             * It can only be asked because something was already understood, so there is no
             * order in flight worth protecting — and the alternative, waiting for the state
             * machine to find its own way back to LISTENING, is seconds of a shopkeeper
             * wondering whether the phone heard him. */
            if (askPending) {
                askPending = false
                clip.clear()
                kws.reset()
                vad.reset()
                Log.i(TAG, "asking: $askLine")
                Bus.emit("capture", "state" to "ask")
                speakAnd(askLine, thenCapture = true)
                continue
            }

            /* Harvesting: transcribe instead of spotting, and do nothing else.
             *
             * Deliberately ahead of every other branch. The point is to see what the model
             * emits for the phrase, so nothing may wake, capture, endpoint or answer while it
             * is on — an acknowledgement playing into the microphone mid-harvest would be
             * collected as though somebody had said it. */
            if (harvesting) {
                val h = harvester ?: Harvester(
                    assets, java.io.File(getExternalFilesDir(null), "harvest.tsv")
                ).also { harvester = it }
                h.accept(floats.copyOf(m))
                continue
            }

            /* The page asked for something to be read out — an item count, a total, a
             * confirmation. Same rule as the acknowledgement, but it can arrive in any state
             * rather than being one, so it is a guard and not a branch. Without it, everything
             * the page says goes into the microphone: announcing "three items" while a clip is
             * open bills the words, and announcing it while listening can wake the phone. */
            if (speaker.speaking) continue

            // Push-to-talk overrides whatever the doorbell was doing.
            if (ptt && state != State.CAPTURING) beginCapture(fromWake = false)
            if (!ptt && state == State.CAPTURING && pttWasTheTrigger) { endCapture(final = true); continue }

            /* One wake may not own the microphone indefinitely.
             *
             * With the room talking, a clip that never endpoints ran to the 12s cap, went
             * to HOLDING, was resumed instantly by the same room, and repeated to MAX_CHAIN
             * — half a minute during which the shopkeeper's own order went into a clip he
             * did not open. The gap test below is what actually stops that; this is the
             * backstop for whatever it does not catch. A genuine chained order endpoints on
             * silence long before this, so it should never fire on real use. */
            if ((state == State.CAPTURING || state == State.HOLDING) &&
                now - wakeStartedAt > WAKE_BUDGET_MS) {
                Log.w(TAG, "wake budget spent (${now - wakeStartedAt}ms) state=$state — standing down")
                if (state == State.CAPTURING && sawGap) endCapture(final = true)
                else { clip.clear(); standDown() }
                continue
            }

            when (state) {
                // Handled above and skipped with a `continue`, because a talking phone must
                // not reach the keyword model at all. Named here only to keep the `when`
                // exhaustive, so that adding a state later is a compile error rather than a
                // frame quietly falling through.
                State.SPEAKING -> {}

                State.IDLE, State.LISTENING -> {
                    if (kws.accept(floats.copyOf(m)) != null) acknowledge()
                }

                State.CAPTURING -> {
                    /* Still listening for the name, but only until he says something.
                     *
                     * Before this the keyword model was fed in exactly one state, so every
                     * wake made the phone deaf to its own name for three seconds — and if
                     * he had spoken, for the four-second continuation window after that.
                     * Saying "Synthia" again in that gap did nothing visible, which is
                     * precisely when a person says it again.
                     *
                     * It stops the moment real speech arrives, so the order itself never
                     * reaches the keyword decoder and cannot re-trigger from inside a
                     * sentence. While the clip is still empty there is nothing to protect,
                     * and hearing the name again means he is starting over. */
                    /* The name is listened for throughout the clip, not just while it is
                     * empty.
                     *
                     * The first attempt at this gated on !heardSpeech, reasoning that once
                     * he had started talking the order should be protected from the
                     * decoder. On a real counter that gate is closed almost immediately:
                     * the VAD marks room noise as speech within a window or two, and the
                     * phone went deaf again — which is the bug, back by another route.
                     *
                     * What he means by saying the name mid-clip is unambiguous: start
                     * over. So nothing is thrown away — whatever he already said is
                     * finished and sent, and a fresh capture opens. If a word inside an
                     * order ever misfires as the name, the cost is an order split across
                     * two clips, and both halves bill to the same bill anyway. */
                    if (kws.accept(floats.copyOf(m)) != null) {
                        if (heardSpeech && now - clipStartedAt >= MIN_CLIP_MS) {
                            endCapture(final = true)
                            beginCapture(fromWake = true)
                        } else {
                            restartCapture()
                        }
                        continue
                    }

                    for (i in 0 until m) clip.add(toPcm(floats[i]))
                    vad.acceptWaveform(floats.copyOf(m))
                    if (vad.isSpeechDetected()) { lastSpeechAt = now; heardSpeech = true }
                    else if (heardSpeech && now - lastSpeechAt > GAP_MS) sawGap = true

                    val clipMs = now - clipStartedAt
                    when {
                        // He said the name and then nothing. Don't send the room.
                        !heardSpeech && clipMs > NO_SPEECH_MS -> abandonCapture()
                        heardSpeech && now - lastSpeechAt > SILENCE_MS -> endCapture(final = false)

                        /* Twelve seconds of unbroken speech is not an order, it is the room.
                         *
                         * A dictated order breathes: the counter recording put the pause
                         * inside one order at 1090ms, which is why SILENCE_MS is 800 — a
                         * real order ends itself on the branch above and almost never
                         * reaches this cap. What reaches it is a VAD held permanently open
                         * by customers, a TV or a fan, and that clip used to be dispatched
                         * (heardSpeech was true, the room having "spoken") and then extended
                         * through HOLDING into the next one. Somebody else's conversation
                         * arrived as line items on a bill.
                         *
                         * So the cap is not an endpoint any more, it is a verdict. With a
                         * pause somewhere in it, treat it as a long order and carry on.
                         * Without one, throw it away and go back to listening for the name. */
                        clipMs > MAX_CLIP_MS ->
                            if (sawGap) endCapture(final = false)
                            else {
                                Log.i(TAG, "clip hit the cap with no pause in it — the room, not an order")
                                abandonCapture()
                            }
                        else -> {}
                    }
                }

                State.HOLDING -> {
                    /* The name outranks the continuation.
                     *
                     * This window exists to catch the second half of an order split by a
                     * pause, and it decided that ANY speech in it was more of the order.
                     * So a repeated "Synthia" was recorded as a bill line, sent to the
                     * recogniser, and came back as an item — while the wake it was actually
                     * meant to be did nothing. Three of those chained up per hold, which is
                     * why a run of failed attempts arrived all at once afterwards.
                     *
                     * Checking the name first makes it unambiguous: hearing it means a new
                     * utterance, not more of the last one. */
                    if (kws.accept(floats.copyOf(m)) != null) {
                        beginCapture(fromWake = true)
                        continue
                    }

                    vad.acceptWaveform(floats.copyOf(m))
                    if (vad.isSpeechDetected() && chain < MAX_CHAIN) {
                        resumeCapture()
                        // The first window of the continuation is speech; keep it.
                        for (i in 0 until m) clip.add(toPcm(floats[i]))
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
        Log.i(TAG, "beginCapture fromWake=$fromWake prevState=$state")
        state = State.CAPTURING
        pttWasTheTrigger = !fromWake
        chain = 1
        clip.clear()
        clipStartedAt = System.currentTimeMillis()
        lastSpeechAt = clipStartedAt
        heardSpeech = false
        sawGap = false
        wakeStartedAt = clipStartedAt
        vad.reset()
        // The doorbell stops ringing before the order is read, so the name can never end
        // up inside the clip and be billed as an item.
        kws.reset()
        feedback(880, 90)
        note(R.string.notif_recording)
        Bus.emit("capture", "state" to "start", "wake" to fromWake)
    }

    /* Answer to his name, and only then start listening.
     *
     * The order is the point. A shopkeeper starts talking the moment he hears that the phone
     * is awake, so a clip opened at the same instant as the reply loses the first word of
     * every order to a sentence the phone was saying itself. The beep still lands
     * immediately — that is the fast, reliable signal — and the words follow it. */
    private fun acknowledge() {
        val name = Bus.ownerName.trim()
        val line = if (name.isEmpty()) getString(R.string.ack)
                   else getString(R.string.ack_named, name)
        Log.i(TAG, "wake acknowledged: $line")
        kws.reset()
        vad.reset()
        feedback(880, 90)
        note(R.string.notif_recording)
        Bus.emit("capture", "state" to "ack")
        /* The pre-rendered one, not a fresh synthesis.
         *
         * This is the only line the shopkeeper waits through — the microphone is deaf until
         * it finishes, by design, so that the answer never lands in the clip. Spoken live it
         * cost 1.3 seconds every time and four seconds on the first wake after launch, which
         * is long enough to swallow the order of anyone who says the name and the item in one
         * breath. Played from a file it is a seek and a start. */
        state = State.SPEAKING
        speakingSince = System.currentTimeMillis()
        speechDone = false
        captureAfterSpeech = true
        speaker.ack(line) { speechDone = true }
    }

    /* Keep the rendered acknowledgement in step with the name.
     *
     * The page pushes the shop's name over the bridge a couple of seconds after it loads, and
     * again whenever it is edited in Settings. Rendering is done off the wake path entirely —
     * the whole point is that nothing is synthesised while somebody is standing there waiting
     * to talk. */
    private fun refreshAck() {
        val name = Bus.ownerName.trim()
        if (name == lastAckName) return
        lastAckName = name
        val line = if (name.isEmpty()) getString(R.string.ack)
                   else getString(R.string.ack_named, name)
        Log.i(TAG, "pre-rendering acknowledgement: $line")
        speaker.prepareAck(line)
    }

    /** Say something, hold the microphone shut until it is finished, then capture or stop. */
    private fun speakAnd(line: String, thenCapture: Boolean) {
        state = State.SPEAKING
        speakingSince = System.currentTimeMillis()
        speechDone = false
        captureAfterSpeech = thenCapture
        speaker.say(line) { speechDone = true }
    }

    /* He said the name again before saying anything else. Start the clip over rather than
     * treating the repeat as content — the old clip holds nothing but room noise. */
    private fun restartCapture() {
        Log.i(TAG, "restartCapture — name heard again before any speech")
        clip.clear()
        clipStartedAt = System.currentTimeMillis()
        lastSpeechAt = clipStartedAt
        heardSpeech = false
        sawGap = false
        wakeStartedAt = clipStartedAt
        vad.reset()
        kws.reset()
        feedback(880, 90)
        Bus.emit("capture", "state" to "start", "wake" to true)
    }

    private fun resumeCapture() {
        Log.i(TAG, "resumeCapture chain=${chain + 1}")
        state = State.CAPTURING
        chain++
        clip.clear()
        clipStartedAt = System.currentTimeMillis()
        lastSpeechAt = clipStartedAt
        sawGap = false
        /* Not `true`, which is what this said.
         *
         * The continuation is opened by one window of VAD speech, and asserting heardSpeech
         * from that alone meant the clip was dispatch-eligible before anything had actually
         * been said into it — so a hold resumed by a passing voice sent the room to the
         * recogniser. The VAD re-confirms within a window or two when it really is more of
         * the order, and when it does not, NO_SPEECH_MS throws the clip away. */
        heardSpeech = false
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

    /* He said the name and then nothing arrived.
     *
     * Silence used to be the entire response: the clip was dropped and the phone went back to
     * listening without a word. With the screen behind him that is indistinguishable from the
     * app being broken, which is the failure this whole feedback layer exists to stop — and
     * it is the one he is most likely to hit, because it happens precisely when the
     * microphone did not pick him up. */
    private fun abandonCapture() {
        clip.clear()
        Bus.emit("capture", "state" to "empty")
        speakAnd(getString(R.string.didnt_catch), thenCapture = false)
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
                Bus.emit("result", res.json.put("native_ms", res.ms).put("clip_ms", ms)
                                           .put("mic", Bus.micLabel))
            } else {
                Bus.emit("voice_error", "reason" to (res.error ?: "unknown"))
            }
        }
    }

    /* ---- mic ---- */

    private fun openMic(): Boolean {
        record?.let {
            if (it.recordingState == AudioRecord.RECORDSTATE_RECORDING) return true
            /* The stream stopped without us asking. Another app took the input — on a real
             * counter that is usually the phone's own assistant hotword firing on customer
             * speech — or the audio server restarted under us.
             *
             * recordingState is latched, so the old code's `return false` was permanent:
             * the loop span at 250ms forever holding a dead AudioRecord that nothing would
             * ever release, because closeMic() only runs when hands-free goes off. That is
             * the whole reason the fix was "switch hands-free off and on from the menu" —
             * the toggle was not resetting the keyword model, it was the only path in the
             * program that freed this object. Drop it here and open a fresh one. */
            Log.w(TAG, "mic stopped underneath us (state=${it.recordingState}) — reopening")
            closeMic()
        }
        return try {
            /* Let the microphone name its own rate.
             *
             * The USB receiver reports 48 kHz / 24-bit and cannot do 16 kHz. Asking for
             * 16 kHz anyway while pinning the route to it produced
             * "setDevice failed to set preferred config" from the policy manager, a stream
             * that worked exactly once, and a keyword model that went deaf the moment the
             * route was rebuilt. Opening at the rate it actually has removes the argument. */
            val target = preferredDevice()
            micRate = chooseRate(target)
            resampler = if (micRate == SAMPLE_RATE) null else Resampler(micRate, SAMPLE_RATE)
            val factor = resampler?.factor ?: 1
            val min = AudioRecord.getMinBufferSize(micRate,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
            val r = AudioRecord(
                // VOICE_RECOGNITION, not MIC: it is the one source whose vendor processing
                // is tuned for speech rather than for a video's soundtrack, and on a cheap
                // handset in a loud room that difference is not subtle.
                MediaRecorder.AudioSource.VOICE_RECOGNITION,
                micRate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                maxOf(min, WINDOW * factor * 8))
            if (r.state != AudioRecord.STATE_INITIALIZED) { r.release(); return false }
            /* Ask for a specific input before starting. Plugging a USB receiver in usually
             * makes Android route to it on its own, but "usually" is not a thing a
             * measurement can rest on: an A/B between three microphones has to be able to
             * say which one was actually recording, and to pin it deliberately rather than
             * hope the platform picked the same one twice. */
            target?.let { r.preferredDevice = it }
            r.startRecording()
            record = r
            // What the platform actually gave us, which is not always what was asked for.
            Bus.micLabel = routedLabel(r)
            Log.i(TAG, "mic open pref=$micPref routed=${Bus.micLabel} " +
                       "rate=$micRate->${SAMPLE_RATE} factor=$factor")
            // Tell the page, so a mic that quietly went missing is visible on the screen
            // he is already looking at rather than only in a log nobody reads in a shop.
            Bus.emit("mic", "pref" to micPref, "routed" to Bus.micLabel,
                     "honoured" to (micPref == "auto" || Bus.micLabel.startsWith(micPref)))
            /* The stream was closed and reopened across a gap of unknown length — the app
             * was backgrounded, or hands-free was off. A streaming zipformer carries left
             * context, and splicing two moments together leaves it decoding against audio
             * that is minutes old. Cheap to drop, and it is the first detection after every
             * resume that would otherwise pay for it. */
            if (this::kws.isInitialized) kws.reset()
            if (this::vad.isInitialized) vad.reset()
            resampler?.reset()
            if (state == State.IDLE) state = State.LISTENING
            true
        } catch (e: SecurityException) {
            Bus.emit("voice_error", "reason" to "no_permission"); false
        }
    }

    /* What rate to open at.
     *
     * SAMPLE_RATE whenever the device can do it, because converting nothing is free and the
     * handset's own microphone has always obliged. Otherwise the lowest rate it offers that
     * is a whole multiple of ours — 48 kHz being three times 16 kHz is what keeps Resampler
     * to a filter and a stride. A device offering only, say, 44.1 kHz would need fractional
     * resampling and does not get it: we open at SAMPLE_RATE and let the platform do
     * whatever it was going to do, which is no worse than before this existed.
     *
     * An empty sampleRates array means the device did not say, which is common and means
     * "anything reasonable" — so it is treated as obliging, not as broken. */
    private fun chooseRate(dev: AudioDeviceInfo?): Int {
        /* The handset's own microphone is opened at SAMPLE_RATE and always has been.
         *
         * It reports {48000} in its supported rates, because that is what the hardware runs
         * at — but AudioRecord has never had any difficulty giving us 16 kHz from it, the
         * platform resamples with the vendor's own tuning, and every measurement behind this
         * app was taken through that path. Reading its rate list and "helpfully" switching it
         * to our own resampler changed the one input that was not broken: the phone-mic
         * harvest ran at factor=1 and the very next launch came up factor=3.
         *
         * The problem this negotiation exists for was never the built-in microphone. It was
         * a USB device that reports 48 kHz / 24-bit, cannot do 16 kHz, and made the audio
         * policy manager say so — so that is the only case that gets the native-rate path. */
        if (dev == null || dev.type == AudioDeviceInfo.TYPE_BUILTIN_MIC) return SAMPLE_RATE
        val rates = try { dev.sampleRates } catch (e: Exception) { null }
        if (rates == null || rates.isEmpty()) return SAMPLE_RATE
        if (rates.contains(SAMPLE_RATE)) return SAMPLE_RATE
        val usable = rates.filter { it > SAMPLE_RATE && it % SAMPLE_RATE == 0 && it <= MAX_RATE }
        val picked = usable.minOrNull()
        if (picked == null) {
            Log.w(TAG, "device offers ${rates.toList()} — none an integer multiple of $SAMPLE_RATE")
            return SAMPLE_RATE
        }
        return picked
    }

    private fun toPcm(f: Float): Short =
        (f * 32768f).toInt().coerceIn(-32768, 32767).toShort()

    /** Coarse on purpose: a category is what an A/B compares, and it is all the page is told. */
    private fun deviceKind(type: Int): String = when (type) {
        AudioDeviceInfo.TYPE_BUILTIN_MIC -> "builtin"
        AudioDeviceInfo.TYPE_WIRED_HEADSET -> "wired"
        AudioDeviceInfo.TYPE_USB_DEVICE,
        AudioDeviceInfo.TYPE_USB_HEADSET,
        AudioDeviceInfo.TYPE_USB_ACCESSORY -> "usb"
        AudioDeviceInfo.TYPE_BLUETOOTH_SCO -> "bt"
        else -> "other"
    }

    private fun preferredDevice(): AudioDeviceInfo? {
        if (micPref == "auto") return null
        val found = try {
            getSystemService(AudioManager::class.java)
                .getDevices(AudioManager.GET_DEVICES_INPUTS)
                .firstOrNull { deviceKind(it.type) == micPref }
        } catch (e: Exception) { null }
        /* Asked for a microphone that is not there.
         *
         * Android's answer to a null preferredDevice is to pick the default, which is the
         * handset's own mic — so the app carried on recording, from the wrong device, and
         * said nothing. A wireless mic that has gone to sleep looks exactly like this: it
         * drops off the device list, the next open silently lands on the phone, and the
         * shopkeeper is talking into a receiver that is no longer in the path. It works,
         * then it does not, and nothing on screen explains why.
         *
         * It still opens — a billing app that refuses to listen because an accessory dozed
         * off is worse than one that listens on the wrong mic — but it is now said out
         * loud, and every clip carries the device that actually recorded it, so the
         * comparison cannot be quietly polluted by the phone's own microphone. */
        if (found == null) Log.w(TAG, "mic pref=$micPref not present — falling back to default")
        return found
    }

    /* The kind, plus the hardware's own name when it has one worth keeping.
     *
     * The built-in mic reports the handset model, which is the phone and not a microphone,
     * so it collapses to the bare kind. A USB receiver reports itself — which is the whole
     * point when the question is whether the wireless mic beat the phone. */
    private fun routedLabel(r: AudioRecord): String {
        val d = try { r.routedDevice } catch (e: Exception) { null } ?: return micPref
        val kind = deviceKind(d.type)
        val name = d.productName?.toString()?.trim().orEmpty()
        return if (name.isEmpty() || name.equals(Build.MODEL, ignoreCase = true)) kind
               else "$kind:$name"
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
        running = false
        alive = false
        worker?.join(1000)
        closeMic()
        uploads.shutdown()
        tones?.release()
        speakerRef = null
        try { unregisterReceiver(harvestSwitch) } catch (_: Exception) {}
        harvester?.release()
        if (this::speaker.isInitialized) speaker.release()
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

        /** How long to wait for TextToSpeech to say it has finished before assuming it has. */
        private const val SPEAK_TIMEOUT_MS = 6000L

        /** A lull inside a clip long enough to call it a pause. Well under SILENCE_MS, so
         *  noticing one never competes with the endpointer — it only records that this clip
         *  has the shape of speech rather than the shape of a room. */
        private const val GAP_MS = 300L

        /** Ceiling on everything one wake may hold: clip, hold and every continuation. */
        private const val WAKE_BUDGET_MS = 30000L

        /** The highest input rate we will open. 48 kHz covers every USB and Bluetooth
         *  microphone worth supporting, and bounds the read buffers above. */
        private const val MAX_RATE = 48000

        /* Heartbeat into logcat. The wake word cannot be tuned from a device you cannot see
         * into, and "nothing happened" has too many causes to guess between.
         *
         * On in release too, and that is deliberate for the pilot. This was BuildConfig.DEBUG,
         * so the build actually in a shop was the one that said nothing — and when a USB
         * microphone streamed hundreds of frames while the keyword model sat silent, the one
         * number that would have separated "no audio" from "wrong audio" was the peak level,
         * which nobody could see. One line every two seconds is a cheap price for that. */
        private const val DEBUG_AUDIO = true

        /** The live voice, for the page to borrow through WebBridge. Null when not running. */
        @Volatile var speakerRef: Speaker? = null
            private set

        /** Set while the service instance exists, so the Activity does not re-start it. */
        @Volatile var running = false
            private set

        private const val PREFS = "voice"
        private const val KEY_HANDS_FREE = "hands_free"
        private const val KEY_MIC = "mic_pref"

        fun handsFreeEnabled(ctx: Context): Boolean =
            ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getBoolean(KEY_HANDS_FREE, false)

        const val ACTION_START = "start"
        const val ACTION_STOP = "stop"
        const val ACTION_PTT_DOWN = "ptt_down"
        const val ACTION_PTT_UP = "ptt_up"
        const val ACTION_SHUTDOWN = "shutdown"
        /** adb shell am broadcast -a com.synthia.app.HARVEST */
        const val ACTION_HARVEST = "com.synthia.app.HARVEST"
        const val ACTION_ASK = "ask"
        const val EXTRA_TEXT = "text"
        const val ACTION_SET_MIC = "set_mic"
        const val EXTRA_MIC = "mic"

        fun micPreference(ctx: Context): String =
            ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY_MIC, "auto") ?: "auto"

        fun send(ctx: Context, action: String) {
            val i = Intent(ctx, VoiceService::class.java).setAction(action)
            ContextCompat.startForegroundService(ctx, i)
        }

        fun ask(ctx: Context, text: String) {
            val i = Intent(ctx, VoiceService::class.java)
                .setAction(ACTION_ASK).putExtra(EXTRA_TEXT, text)
            ContextCompat.startForegroundService(ctx, i)
        }

        fun setMic(ctx: Context, pref: String) {
            val i = Intent(ctx, VoiceService::class.java)
                .setAction(ACTION_SET_MIC).putExtra(EXTRA_MIC, pref)
            ContextCompat.startForegroundService(ctx, i)
        }
    }
}
