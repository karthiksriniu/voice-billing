package com.synthia.app

import android.content.res.AssetManager
import android.util.Log
import com.k2fsa.sherpa.onnx.EndpointConfig
import com.k2fsa.sherpa.onnx.FeatureConfig
import com.k2fsa.sherpa.onnx.OnlineModelConfig
import com.k2fsa.sherpa.onnx.OnlineRecognizer
import com.k2fsa.sherpa.onnx.OnlineRecognizerConfig
import com.k2fsa.sherpa.onnx.OnlineStream
import com.k2fsa.sherpa.onnx.OnlineTransducerModelConfig

/* Finding out what the model hears when THIS person says the name, through THIS microphone.
 *
 * android/README.md has described this method since the wake word was chosen, and every
 * keyword currently registered was produced by it — but from synthesized voices with pink
 * noise mixed in, never from a human being. The README says so plainly and says nothing is
 * tuned until it has been redone with real recordings. This is the tool for redoing it.
 *
 * The evidence that it is needed came off the pilot phone. Eight wakes in one session
 * decoded as three different things — HEY SYNTHIA four times, HAS SYNTHIA four times,
 * SYNTHIA twice — for a man saying the same two words into the same microphone. HAS SYNTHIA
 * is not a phrase anybody said; it is what the model emits for "Hey Synthia" in his voice,
 * and it fires only because a harvest from synthetic speech happened to include it. Every
 * decode that landed outside the five registered sequences was silence, and from the
 * counter that is a wake word that works most of the time for no visible reason.
 *
 * The trick is that the keyword model is a tiny ASR wearing a different hat. Loaded as an
 * OnlineRecognizer instead of a KeywordSpotter — the same three files, the same tokens, the
 * same features — it will happily transcribe, and what it emits IS the keyword, verbatim.
 * Guessing the spelling does not work and never has: the model has never heard an Indian
 * name and decodes one into whatever English subwords fit.
 *
 * Fed from VoiceService's own pipeline, so what reaches it has been through the real
 * microphone, the real 48kHz-to-16kHz conversion and the real room. A harvest done on clean
 * audio through a different path would describe a phone nobody owns.
 *
 * Output is a logcat line per utterance, in exactly the format keywords.txt takes.
 */
class Harvester(assets: AssetManager) {

    private val recognizer: OnlineRecognizer
    private var stream: OnlineStream
    private var seen = LinkedHashMap<String, Int>()

    init {
        val model = OnlineModelConfig(
            transducer = OnlineTransducerModelConfig(
                encoder = "$DIR/encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
                decoder = "$DIR/decoder-epoch-12-avg-2-chunk-16-left-64.onnx",
                joiner = "$DIR/joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
            ),
            tokens = "$DIR/tokens.txt",
            numThreads = 1,
            provider = "cpu",
            modelType = "zipformer2",
        )
        recognizer = OnlineRecognizer(
            assetManager = assets,
            config = OnlineRecognizerConfig(
                featConfig = FeatureConfig(sampleRate = Kws.SAMPLE_RATE, featureDim = 80, dither = 0f),
                modelConfig = model,
                // Endpointing is what splits one "Hey Synthia" from the next. Deliberately
                // impatient: the phrase is two words and the pauses between attempts are long,
                // so a short trailing silence segments them cleanly without merging two
                // attempts into one sequence that matches neither.
                endpointConfig = EndpointConfig(),
                enableEndpoint = true,
                decodingMethod = "greedy_search",
            ),
        )
        stream = recognizer.createStream()
        Log.i(TAG, "HARVEST MODE — say the wake phrase; every decode is printed below")
    }

    /* Loudest sample since the last decode.
     *
     * The first harvest was unreadable without this. Eleven of its forty-one decodes were
     * fluent English nobody in the room had said to the phone — "YOU KNOW WHAT", "I LOVE MY
     * SON", "MAKE THEM CHANGE" — which is a television, and the wake attempts were mixed in
     * with it at whatever level they happened to arrive. With no way to tell a close-talked
     * phrase from the far wall, every sequence had to be treated as equally real, and the
     * ones worth registering could not be told from the ones that would fire on the room. */
    private var peak = 0f

    /** Feed one 16 kHz window. Prints a line whenever an utterance ends. */
    fun accept(samples: FloatArray) {
        for (v in samples) { val a = kotlin.math.abs(v); if (a > peak) peak = a }
        stream.acceptWaveform(samples, Kws.SAMPLE_RATE)
        while (recognizer.isReady(stream)) recognizer.decode(stream)
        if (!recognizer.isEndpoint(stream)) return

        val res = recognizer.getResult(stream)
        val text = res.text.trim()
        val tokens = res.tokens.joinToString(" ").trim()
        recognizer.reset(stream)
        if (tokens.isEmpty()) return

        val n = (seen[tokens] ?: 0) + 1
        seen[tokens] = n
        val lvl = (peak * 32768f).toInt()
        peak = 0f
        // The token line is the deliverable: paste it straight into keywords.txt. The text is
        // only there to make it obvious which attempts were the phrase and which were the
        // room, and the level is what settles it when the text alone cannot.
        Log.i(TAG, "HARVEST x$n  peak=$lvl  \"$tokens\"   (heard as: $text)")
    }

    /** Everything collected so far, commonest first — the shortlist to register. */
    fun summary(): String =
        seen.entries.sortedByDescending { it.value }
            .joinToString("\n") { "  x${it.value}  \"${it.key}\"" }

    fun release() {
        stream.release()
        recognizer.release()
    }

    companion object {
        private const val TAG = "Harvest"
        private const val DIR = "kws"
    }
}
