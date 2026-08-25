package com.bolobill.app

import android.content.res.AssetManager
import android.util.Log
import com.k2fsa.sherpa.onnx.FeatureConfig
import com.k2fsa.sherpa.onnx.KeywordSpotter
import com.k2fsa.sherpa.onnx.KeywordSpotterConfig
import com.k2fsa.sherpa.onnx.OnlineModelConfig
import com.k2fsa.sherpa.onnx.OnlineStream
import com.k2fsa.sherpa.onnx.OnlineTransducerModelConfig

/* The doorbell.
 *
 * A 3.3M-parameter zipformer transducer decoded against a fixed keyword list — small
 * enough to run on every frame all day, and useless for anything except answering to its
 * name, which is exactly the privacy property we want: the model that is always listening
 * physically cannot transcribe the room.
 *
 * ---------------------------------------------------------------------------------------
 * The keywords below are NOT spellings of "Akhila". They are what this model actually
 * emits when it hears the phrase, and the difference is the whole reason the first wake
 * word had to be abandoned.
 *
 * The model was trained on gigaspeech: English, and no Indian names in it. Asked to spot a
 * name it has never heard, it does not fail politely — it decodes the sound into whatever
 * English subwords fit, and the constrained decoder then looks for token sequences that are
 * never produced. The previous name, "Vishwa Bill", was unspottable for exactly this
 * reason: across fourteen synthesized voices the model heard FISH WERE BILL (seven times),
 * VISHUA BILL, ISSUE A BILL, WISH MY BILL. Thirty-two hand-written spellings of "Vishwa"
 * were tried against it and not one fired, at any threshold.
 *
 * So the list is harvested, not written. The method — and it is the part worth keeping,
 * because it has to be re-run against real voices:
 *
 *   1. Record the phrase (many speakers, several speeds).
 *   2. Load THESE SAME model files as an OnlineRecognizer instead of a KeywordSpotter —
 *      the KWS model is a tiny ASR, so it will happily transcribe.
 *   3. Take the emitted token sequences verbatim. Those are the keywords.
 *
 * What is here came from step 3 over 24 synthesized clips (8 voices x 3 speeds, including
 * the Indian-English voices), and fires on 24/24 at the score and threshold below.
 *
 * The honest caveat: synthesized speech is not a shopkeeper. This list is what makes the
 * wake word work today, not what makes it correct. It must be re-harvested from real
 * recordings of real people saying "Hey Akhila" in a real shop, and the false-accept rate
 * measured against hours of real shop noise, before any of these numbers are trusted.
 * Several variants below ("HEY ACTUALLY", "HERE KILLER") are plainly things a person could
 * say by accident — in an English-speaking room they would be a problem, and in a Tamil
 * one they are unlikely enough to accept for a pilot.
 * ---------------------------------------------------------------------------------------
 */
class Kws(assets: AssetManager) {

    private val spotter: KeywordSpotter
    private var stream: OnlineStream

    init {
        val model = OnlineModelConfig(
            transducer = OnlineTransducerModelConfig(
                encoder = "$DIR/encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
                decoder = "$DIR/decoder-epoch-12-avg-2-chunk-16-left-64.onnx",
                joiner = "$DIR/joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
            ),
            tokens = "$DIR/tokens.txt",
            numThreads = 1,          // it runs on every frame; leave the phone a core
            provider = "cpu",
            modelType = "zipformer2",
        )
        val config = KeywordSpotterConfig(
            featConfig = FeatureConfig(sampleRate = SAMPLE_RATE, featureDim = 80, dither = 0f),
            modelConfig = model,
            maxActivePaths = 4,
            keywordsFile = "$DIR/keywords.txt",
            keywordsScore = KEYWORD_SCORE,
            keywordsThreshold = KEYWORD_THRESHOLD,
            numTrailingBlanks = 2,
        )
        spotter = KeywordSpotter(assetManager = assets, config = config)
        stream = spotter.createStream(KEYWORDS)
    }

    /**
     * Feed one window of audio. Returns the keyword if this window completed one.
     *
     * Detection lands at the END of the name, which is what lets the caller start
     * recording the order immediately without the name landing inside the clip and being
     * billed as an item.
     */
    fun accept(samples: FloatArray): String? {
        stream.acceptWaveform(samples, SAMPLE_RATE)
        while (spotter.isReady(stream)) spotter.decode(stream)
        val keyword = spotter.getResult(stream).keyword
        if (keyword.isEmpty()) return null
        // Without this the same utterance keeps re-firing for as long as it stays in the
        // decoder's context, and one "Bolo Bill" opens four clips.
        spotter.reset(stream)
        Log.i(TAG, "wake: $keyword")
        return keyword
    }

    /** Drop everything heard so far — used when we stop listening for the name. */
    fun reset() = spotter.reset(stream)

    fun release() {
        stream.release()
        spotter.release()
    }

    companion object {
        private const val TAG = "Kws"
        private const val DIR = "kws"
        const val SAMPLE_RATE = 16000

        /* Chosen by sweeping both against the 24-clip set: (1.5, 0.25) missed two of the
         * Indian-English renditions, (2.0, 0.15) missed none. Score boosts the keyword path
         * in the decoder; threshold is the acceptance bar. Loosening either trades misses
         * for false accepts, and which way to move is a question only shop-noise recordings
         * can answer. */
        const val KEYWORD_SCORE = 2.0f
        const val KEYWORD_THRESHOLD = 0.15f

/** What the model emits for "Hey Akhila". Harvested, not spelled — see above. */
        val KEYWORDS = listOf(
            "▁THEY ▁A C C U LA",        // x6 of 24
            "▁HE Y ▁ACTUALLY",          // x3
            "▁HE Y ▁ ACT ▁HI LL AR",    // x3
            "▁HE Y ▁A C C UL AR",       // x3
            "▁YEAH ▁A ▁K IL LA",        // x2
            "▁HERE ▁K I LL ER",         // x2
            "▁YEAH ▁K I LL AR",         // x1
            "▁HE ▁ N U CK Y LA",        // x1
            "▁HE ▁A R CH IL LA",        // x1
            "▁HE ▁K N O CK IL LA",      // x1
            "▁YEAH ▁K I LL ER",         // x1
        ).joinToString("\n")
    }
}
