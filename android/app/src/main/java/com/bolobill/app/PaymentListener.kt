package com.bolobill.app

import android.app.Notification
import android.content.ComponentName
import android.content.Context
import android.provider.Settings
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import android.util.Log

/* Did the money actually arrive?
 *
 * D5 settled on the shopkeeper tapping "Received", because Play Store policy put
 * NotificationListenerService out of reach and an app whose core loop depends on a
 * restricted permission gets removed. Sideloading removes that constraint, and D5's own
 * reverse clause names this as the right answer the moment it does.
 *
 * Two boundaries, both deliberate:
 *
 *   This class REPORTS. It never confirms a bill. It reads an amount out of a notification
 *   the shopkeeper's own UPI app posted and hands it to the page, which matches it against
 *   the open total and shows it for a human to accept. A notification is text written by
 *   another application; treating it as an instruction to mark money received is how you
 *   get a bill settled by a refund alert, and wrong bills are the one thing the product
 *   says it will not do.
 *
 *   Nothing is stored and nothing leaves the phone. We do not keep a payment log, we do not
 *   read notifications from anything that is not on the list below, and the payer's name is
 *   never forwarded — only the amount. The permission grants access to every notification
 *   on the device, which is exactly why the filter is the first thing that happens.
 */
class PaymentListener : NotificationListenerService() {

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        if (sbn.packageName !in UPI_APPS) return

        val extras = sbn.notification.extras
        val text = listOfNotNull(
            extras.getCharSequence(Notification.EXTRA_TITLE)?.toString(),
            extras.getCharSequence(Notification.EXTRA_TEXT)?.toString(),
            extras.getCharSequence(Notification.EXTRA_BIG_TEXT)?.toString(),
        ).joinToString(" ")
        if (text.isBlank()) return

        val lower = text.lowercase()
        // Money in, not money out. Without this a payment the shopkeeper MAKES settles the
        // customer's bill.
        if (INBOUND.none { it in lower }) return
        if (OUTBOUND.any { it in lower }) return

        val amount = AMOUNT.find(text)?.groupValues?.get(1)?.replace(",", "")?.toDoubleOrNull() ?: return

        Log.i(TAG, "payment signal: ${sbn.packageName} ₹$amount")
        Bus.emit("payment",
            "amount" to amount,
            "app" to sbn.packageName,
            "at" to System.currentTimeMillis())
    }

    companion object {
        private const val TAG = "PaymentListener"

        /** Only these. The permission is device-wide; the code is not. */
        private val UPI_APPS = setOf(
            "com.google.android.apps.nbu.paisa.user",   // Google Pay
            "com.phonepe.app",                          // PhonePe
            "net.one97.paytm",                          // Paytm
            "in.org.npci.upiapp",                       // BHIM
            "in.amazon.mShop.android.shopping",         // Amazon Pay
        )

        private val INBOUND = listOf("received", "credited", "you got", "paid you", "வந்தது")
        private val OUTBOUND = listOf("you paid", "debited", "sent to", "payment of", "requested")

        /** ₹1,234.50 / Rs. 1234 / INR 1234 */
        private val AMOUNT = Regex("""(?:₹|Rs\.?|INR)\s?([0-9][0-9,]*(?:\.[0-9]{1,2})?)""",
            RegexOption.IGNORE_CASE)

        fun isEnabled(ctx: Context): Boolean {
            val flat = Settings.Secure.getString(ctx.contentResolver,
                "enabled_notification_listeners") ?: return false
            val me = ComponentName(ctx, PaymentListener::class.java)
            return flat.split(":").any {
                ComponentName.unflattenFromString(it)?.packageName == me.packageName
            }
        }
    }
}
