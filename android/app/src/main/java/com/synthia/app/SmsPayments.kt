package com.synthia.app

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.provider.Telephony
import android.util.Log

/* Did the money actually arrive? Ask the bank, not the app.
 *
 * This replaces reading a specific UPI app's notifications, and it is the better source for
 * a reason that has nothing to do with convenience: the bank's credit SMS is the same
 * message whichever app the customer paid from. GPay, PhonePe, Paytm, a QR scanned in a
 * banking app the shopkeeper has never heard of — they all end in one SMS from his own
 * bank. A notification listener had to know every payer app in India and be wrong about
 * the ones it did not know.
 *
 * Both permissions this needs are Play Store policy violations for our use case, and we are
 * only allowed them because we are not shipping through Play. That is D5's reverse clause
 * firing, and it is worth being precise about what it buys and what it costs.
 *
 * The boundaries are unchanged from the notification reader, and they matter more here
 * because SMS is a far more sensitive stream than one app's notifications:
 *
 *   Nothing is stored. Not the message, not the sender, not a payment log. The amount is
 *   read out of the text and handed to the page; the SMS itself is never written anywhere
 *   by us and never leaves the phone.
 *
 *   Nothing that is not a credit is looked at twice. A message has to name a rupee amount
 *   AND say it was credited AND not say it was debited. OTPs, promotions, delivery
 *   updates and the shopkeeper's own outgoing payments all fall out here.
 *
 *   This REPORTS. It never settles a bill. The page matches the amount against the open
 *   total and a human still says yes. An SMS is text written by somebody else — a refund
 *   alert, a salary credit, a message from a person quoting a number — and a bill that
 *   closes itself on one is precisely the silent error the product says it will not make.
 */
class SmsPayments : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Telephony.Sms.Intents.SMS_RECEIVED_ACTION) return

        // A long SMS arrives as several parts; the amount can straddle the split.
        val body = Telephony.Sms.Intents.getMessagesFromIntent(intent)
            ?.joinToString(" ") { it.displayMessageBody ?: "" }
            ?.trim()
            .orEmpty()
        if (body.isBlank()) return

        val amount = creditedAmount(body) ?: return

        Log.i(TAG, "credit SMS: ₹$amount")
        Bus.emit("payment",
            "amount" to amount,
            "source" to "sms",
            "at" to System.currentTimeMillis())
    }

    companion object {
        private const val TAG = "SmsPayments"

        /* Indian bank credit SMS has settled into a recognisable shape over the years:
         *   "Rs.120.00 credited to A/c XX1234 via UPI from ..."
         *   "INR 45 credited to your account ... UPI Ref no ..."
         *   "Received Rs 250 in your A/c ..."
         * The amount pattern is deliberately strict about the currency marker, because a
         * bare number in an OTP message would otherwise qualify. */
        private val AMOUNT = Regex(
            """(?:₹|\bRs\.?\s?|\bINR\s?)\s?([0-9][0-9,]*(?:\.[0-9]{1,2})?)""",
            RegexOption.IGNORE_CASE)

        private val CREDIT = listOf("credited", "received", "credit of", "deposited", "has been credited")
        private val DEBIT = listOf("debited", "debit of", "withdrawn", "spent", "paid to", "sent to",
                                   "payment of", "purchase of")
        /* An OTP message very often also carries an amount ("OTP for txn of Rs 500"), and
         * acting on one would close a bill the customer has not paid yet. */
        private val NOT_A_PAYMENT = listOf("otp", "one time password", "do not share",
                                           "request", "requested", "will be debited",
                                           "failed", "declined", "reversed", "refund")

        /** The credited rupee amount, or null if this is not a credit we should act on. */
        fun creditedAmount(body: String): Double? {
            val lower = body.lowercase()
            if (NOT_A_PAYMENT.any { it in lower }) return null
            if (DEBIT.any { it in lower }) return null
            if (CREDIT.none { it in lower }) return null
            return AMOUNT.find(body)?.groupValues?.get(1)?.replace(",", "")?.toDoubleOrNull()
        }
    }
}
