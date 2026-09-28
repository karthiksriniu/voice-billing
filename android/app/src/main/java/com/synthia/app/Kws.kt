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

        /* Swept against positives at four noise levels and 72 negatives. Loosening these
         * does NOT buy noise robustness — 5 dB went 3/24 to 4/24 across the whole usable
         * range while false accepts started appearing, which is what sent the fix to the
         * keyword list instead. Left at the permissive end of the clean-audio sweep. */
        const val KEYWORD_SCORE = 2.0f
        const val KEYWORD_THRESHOLD = 0.15f

/** What the model emits for the name. Harvested, not spelled — see above.
         *
         * The name is SAHANA, and it replaced SYNTHIA on measurement rather than taste.
         *
         * Every keyword before this came from synthesized voices with pink noise mixed in,
         * and the README has said since the day it was written that nothing is tuned until
         * it has been re-harvested from a human being. Harvested from one, on the pilot
         * phone, through the microphone that will actually be used:
         *
         *   "Synthia"  0 of 3 attempts produced ▁S Y N TH IA. It came back as HAZYNTHEO,
         *              SY, PAY SYMPIER. Through the wireless mic earlier the same evening,
         *              1 of 16 — the rest were HAS INDIA, HASN'T DEAR, PASSING GEM,
         *              CASINDOW. The premise that "the model simply knows the word" was
         *              true of synthetic speech and not of this speaker.
         *   "Sahana"   3 of 8, and all three identical: ▁SA HA N A, transcribed as SAHANA
         *              outright. The five misses are degraded rather than scattered — SA HA,
         *              SA M H, HA N N A — which is a level problem, not a decoding one.
         *
         * ("Alexa" was 5 of 5, including at a peak of 1286 where everything else fell apart.
         * It is what a word the model has heard a million times looks like, and it is the bar
         * neither of ours reaches. Obviously unusable: it is Amazon's, and it would set off
         * every Echo within earshot.)
         *
         * REGISTERED: only sequences that contain SA HA and could not be something a customer
         * says. The fragments harvested at very low level — ▁HA N N A ("Hanna", a person's
         * name), ▁SO ▁HA N N A, ▁SA HA on its own — are deliberately left out. Registering
         * them would buy back the quiet attempts at the price of waking on somebody's name,
         * which is the trade that put twelve bad keywords in this list the first time.
         *
         * NOT YET HARVESTED: a carrier. "Hey Synthia" was worth 14 of 24 against 3 at 5 dB
         * SNR where the bare name collapsed, so "Hey Sahana" is very likely the largest win
         * still available and it needs its own harvest — ▁HI and ▁HE Y are different tokens
         * and neither can be guessed from the spelling.
         */
        val KEYWORDS = listOf(
            "▁SA HA N A",        // x3 of 8 — transcribed as SAHANA outright, at every level
            "▁SA HA ▁NO",        // a quiet attempt; still carries SA HA, still not English
        ).joinToString("\n")
    }
}
