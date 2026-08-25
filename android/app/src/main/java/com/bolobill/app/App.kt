package com.bolobill.app

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager

class App : Application() {
    override fun onCreate() {
        super.onCreate()
        val nm = getSystemService(NotificationManager::class.java)
        // IMPORTANCE_LOW: the shopkeeper needs to be able to see that the mic is on, and
        // needs to never be pinged about it.
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_VOICE, getString(R.string.channel_voice),
                NotificationManager.IMPORTANCE_LOW)
        )
    }

    companion object { const val CHANNEL_VOICE = "voice" }
}
