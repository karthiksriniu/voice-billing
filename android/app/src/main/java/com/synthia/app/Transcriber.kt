package com.synthia.app

import android.util.Log
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/* Clip out, line items back.
 *
 * This is the PoC's cloud hop and it is the one part of the pipeline that the product is
 * committed to deleting: cloud ASR cannot survive the cost ceiling (PLAN.md, D1). It lives
 * behind one function for that reason — when the on-device backend wins the Phase 1
 * bakeoff, VoiceService keeps its state machine and this file is replaced, not edited.
 *
 * It runs natively rather than in the page because the microphone now outlives the WebView:
 * a clip must still reach the recogniser when the screen has gone dark mid-order. */
class Transcriber(private val base: String) {

    private val http = OkHttpClient.Builder()
        .callTimeout(20, TimeUnit.SECONDS)
        .build()

    /** Blocking. Called from the audio pipeline's own worker, never the audio thread. */
    fun send(wav: ByteArray, clipMs: Long): Result {
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("audio", "clip.wav", wav.toRequestBody("audio/wav".toMediaType()))
            .addFormDataPart("shop_id", Bus.shopId)
            .addFormDataPart("mode", Bus.mode)
            .addFormDataPart("lang", Bus.lang)
            // The actual input device, not the literal "native" this used to send — with
            // every clip tagged the same, the log could not compare two microphones.
            .addFormDataPart("mic", Bus.micLabel)
            .addFormDataPart("clip_ms", clipMs.toString())
            .build()
        val req = Request.Builder().url("$base/api/transcribe").post(body).build()

        val t0 = System.currentTimeMillis()
        return try {
            http.newCall(req).execute().use { res ->
                val text = res.body?.string().orEmpty()
                if (!res.isSuccessful) {
                    Log.w(TAG, "transcribe HTTP ${res.code}: ${text.take(200)}")
                    Result(null, System.currentTimeMillis() - t0, "http_${res.code}")
                } else {
                    Result(JSONObject(text), System.currentTimeMillis() - t0, null)
                }
            }
        } catch (e: Exception) {
            // Offline is the normal case in a shop, not an exception. Say so quietly and
            // let the caller decide what the shopkeeper sees.
            Log.w(TAG, "transcribe failed: ${e.message}")
            Result(null, System.currentTimeMillis() - t0, "network")
        }
    }

    data class Result(val json: JSONObject?, val ms: Long, val error: String?)

    companion object { private const val TAG = "Transcriber" }
}
