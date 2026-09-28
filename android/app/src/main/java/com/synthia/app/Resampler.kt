package com.synthia.app

import kotlin.math.PI
import kotlin.math.sin

/* 48 kHz in, 16 kHz out — because the microphone decides the rate, not us.
 *
 * Everything downstream of the audio thread is fixed at 16 kHz: the keyword model's
 * features, Silero's 512-sample window, and the WAV that goes to the recogniser. The phone's
 * own microphone gives us that rate directly, so for two years nothing ever had to think
 * about it.
 *
 * A USB microphone does not. The one this was written for reports 48 kHz, one channel,
 * 24-bit PCM, and asking AudioRecord for 16/16 while pinning the route to it made the
 * platform say so out loud:
 *
 *     E/APM::AudioInputDescriptor: setDevice failed to set preferred config
 *                                  for device AUDIO_DEVICE_IN_USB_DEVICE
 *
 * It then resampled anyway, and the result worked — once. After the first bill the capture
 * route was torn down and rebuilt (playing the confirmation beep through the speaker is
 * enough to do it), the stream came back, the USB frames kept arriving by the hundred, and
 * the keyword model never fired again. A microphone that works until the first beep and is
 * then deaf until it is physically re-plugged.
 *
 * So we stop asking the device to be something it is not. Open at whatever rate it actually
 * runs at, and do the conversion here, where it is ours and it is visible.
 *
 * 48000/16000 is exactly 3, which is the whole reason this file is short: no fractional
 * phase accumulator, no interpolation, just a low-pass and every third sample. The filter is
 * not optional — decimating without one folds everything above 8 kHz back down on top of the
 * speech, and the keyword model is looking at exactly that band to tell SYNTHIA from CYNTHIA.
 */
class Resampler(inputRate: Int, outputRate: Int) {

    val factor = inputRate / outputRate

    /** True when the device already gives us what we want and this class should be skipped. */
    val isIdentity = factor == 1 && inputRate == outputRate

    init {
        require(inputRate % outputRate == 0) {
            "only integer decimation is supported; got $inputRate -> $outputRate"
        }
    }

    /* A windowed-sinc low-pass at the output Nyquist, which for 48->16 is 8 kHz.
     *
     * Thirty-one taps is a deliberate choice rather than a default: it runs in well under a
     * millisecond per 32ms window on the cheapest phone we target, and its transition band
     * is narrow enough that the fricatives the wake word depends on survive. The name starts
     * with /s/, and /s/ lives almost entirely above 4 kHz — blunt the top of the band and the
     * model stops hearing the difference between the name and the word next to it. */
    private val taps: FloatArray = run {
        val n = 31
        val cutoff = 0.5f / factor            // normalised to the input rate
        val mid = (n - 1) / 2
        val h = FloatArray(n)
        var sum = 0f
        for (i in 0 until n) {
            val k = (i - mid).toFloat()
            val sinc = if (k == 0f) 2f * cutoff
                       else (sin(2.0 * PI * cutoff * k) / (PI * k)).toFloat()
            // Hamming. Plain truncation rings badly enough to matter at this few taps.
            val w = (0.54 - 0.46 * kotlin.math.cos(2.0 * PI * i / (n - 1))).toFloat()
            h[i] = sinc * w
            sum += h[i]
        }
        for (i in 0 until n) h[i] /= sum      // unity gain at DC, so levels are unchanged
        h
    }

    /* The filter's memory between calls.
     *
     * Audio arrives in windows, and a FIR needs the samples either side of the one it is
     * computing. Starting each buffer from zero would put a click at every window boundary —
     * 31 of them a second, which is a periodic signal the VAD would happily call speech. */
    private val history = FloatArray(taps.size - 1)

    /**
     * Filter and decimate one buffer.
     *
     * [n] input samples yield exactly `n / factor` output samples, so the caller reads a
     * multiple of [factor] and gets a fixed-size window back.
     */
    fun process(input: FloatArray, n: Int, output: FloatArray): Int {
        var written = 0
        var i = 0
        while (i + factor <= n) {
            // Centre the filter on this output sample's position in the input stream.
            var acc = 0f
            for (t in taps.indices) {
                val idx = i + factor - 1 - t
                acc += taps[t] * if (idx >= 0) input[idx] else history[history.size + idx]
            }
            output[written++] = acc
            i += factor
        }
        // Carry the tail forward so the next buffer can reach back across the boundary.
        val keep = history.size
        for (k in 0 until keep) {
            val idx = n - keep + k
            history[k] = if (idx >= 0) input[idx] else history[history.size + idx]
        }
        return written
    }

    /** Forget the boundary samples — used whenever the stream is reopened. */
    fun reset() = history.fill(0f)
}
