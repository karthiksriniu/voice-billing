package com.synthia.app

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
 * The keywords below are what the model EMITS for the name, not how the name is spelled.
 * Keywords match as exact BPE token sequences, so a keyword written from the spelling
 * fires only by luck. Two earlier names are the evidence:
 *
 *   "Vishwa Bill"  — unspottable. Across fourteen voices the model heard FISH WERE BILL
 *                    (7x), VISHUA BILL, ISSUE A BILL, WISH MY BILL. Thirty-two hand-written
 *                    spellings were tried at every threshold; none ever fired.
 *   "Hey Akhila"   — workable but scattered: eleven distinct sequences over 24 clips, the
 *                    most common covering only six of them.
 *   "Synthia"      — the model simply knows the word. Seventeen of 24 clips land on the
 *                    identical sequence and transcribe as SYNTHIA outright.
 *
 * That last point is why this name is worth more than the branding change that motivated
 * it. A wake word the acoustic model already has in its vocabulary needs no luck: the
 * variants below are the accents that drift off it, not a search for something that works.
 *
 * The method, for when the phrase or the speaker population changes — load THESE SAME
 * model files as an OnlineRecognizer instead of a KeywordSpotter (the KWS model is a tiny
 * ASR and will happily transcribe), say the phrase many ways, and keep the emitted token
 * sequences verbatim.
 *
 * Measured over 24 synthesized clips (8 voices x 3 speeds, Indian-English included) and 36
 * negatives of shop speech — "two kilo sugar", "close the bill", "cash received", "sixty
 * five rupees": 24/24 wake, 0/36 false accepts, at the score and threshold below. It still
 * has never heard a human being in a loud room, and that is the number that decides it.
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

        /* Swept against the 24 positives and 36 negatives. This name has headroom the
         * previous two did not: it is 24/24 with zero false accepts here, and still 23/24
         * with zero at the much looser (1.0, 0.35). The permissive end is chosen on
         * purpose — real audio is harder than synthesized audio, and the measured false
         * accepts are the thing we have room to spend. */
        const val KEYWORD_SCORE = 2.0f
        const val KEYWORD_THRESHOLD = 0.15f

/** What the model emits for "Synthia". Harvested, not spelled — see above. */
        val KEYWORDS = listOf(
            "▁S Y N TH IA",       // x17 of 24 — the model transcribes the name outright
            "▁C IN TI ER",        // x2
            "▁S IN VI A",         // x1
            "▁S IN TE ▁A IR",     // x1
            "▁S IN K I ER",       // x1
            "▁S Y N TE ER",       // x1
            "▁S IN CE RE",        // x1
        ).joinToString("\n")
    }
}
