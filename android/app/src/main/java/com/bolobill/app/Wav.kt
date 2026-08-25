package com.bolobill.app

import java.io.ByteArrayOutputStream

/* 16-bit PCM in, RIFF out.
 *
 * The page sent webm/opus because a browser has no other choice. We have the raw frames,
 * and a 12-second clip is ~380 KB as WAV — irrelevant on the counter's wifi and one less
 * encoder between the shopkeeper's voice and the recogniser. When ASR moves on-device this
 * whole file stops being needed. */
object Wav {

    fun encode(pcm: ShortArray, sampleRate: Int = Kws.SAMPLE_RATE): ByteArray {
        val dataBytes = pcm.size * 2
        val out = ByteArrayOutputStream(44 + dataBytes)

        fun ascii(s: String) = out.write(s.toByteArray(Charsets.US_ASCII))
        fun le32(v: Int) = out.write(byteArrayOf(
            (v and 0xff).toByte(), ((v shr 8) and 0xff).toByte(),
            ((v shr 16) and 0xff).toByte(), ((v shr 24) and 0xff).toByte()))
        fun le16(v: Int) = out.write(byteArrayOf((v and 0xff).toByte(), ((v shr 8) and 0xff).toByte()))

        ascii("RIFF"); le32(36 + dataBytes); ascii("WAVE")
        ascii("fmt "); le32(16); le16(1); le16(1)          // PCM, mono
        le32(sampleRate); le32(sampleRate * 2); le16(2); le16(16)
        ascii("data"); le32(dataBytes)

        val bytes = ByteArray(dataBytes)
        for (i in pcm.indices) {
            bytes[i * 2] = (pcm[i].toInt() and 0xff).toByte()
            bytes[i * 2 + 1] = ((pcm[i].toInt() shr 8) and 0xff).toByte()
        }
        out.write(bytes)
        return out.toByteArray()
    }
}
