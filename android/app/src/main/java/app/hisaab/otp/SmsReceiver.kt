package app.hisaab.otp

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.provider.Telephony

/**
 * Sees each SMS as it arrives (RECEIVE_SMS; the inbox is never read). A text is forwarded only when the app is
 * paired, the sender is on the allowlist and the body holds exactly one code. Anything else is dropped here and now,
 * and nothing about any SMS is ever logged.
 */
class SmsReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Telephony.Sms.Intents.SMS_RECEIVED_ACTION) return
        val parts = Telephony.Sms.Intents.getMessagesFromIntent(intent) ?: return
        if (parts.isEmpty()) return
        val store = try {
            Store(context)
        } catch (e: Exception) {
            return
        }
        val token = store.token ?: return
        val filter = OtpFilter(store.senders)

        // A long SMS arrives in parts: join them per sender.
        val keep = parts.filter { it.originatingAddress != null }
            .groupBy { it.originatingAddress!! }
            .mapNotNull { (sender, msgs) ->
                val body = msgs.joinToString("") { it.messageBody ?: "" }
                if (filter.shouldForward(sender, body)) Triple(sender, body, msgs.first().timestampMillis) else null
            }
        val stale = System.currentTimeMillis() - store.configFetchedAt > DAY_MS
        if (keep.isEmpty() && !stale) return

        val pending = goAsync()
        Thread {
            try {
                run(store, token, keep, stale)
            } finally {
                pending.finish()
            }
        }.start()
    }

    private fun run(store: Store, token: String, keep: List<Triple<String, String, Long>>, stale: Boolean) {
        val server = store.server
        for ((sender, body, ts) in keep) {
            try {
                Api.forward(server, token, sender, body, ts)
            } catch (e: Api.Unauthorized) {
                store.clear() // revoked from the web: stop forwarding at once
                return
            } catch (e: Exception) {
                // dropped; never retried, never logged
            }
        }
        if (stale) {
            try {
                store.saveSenders(Api.config(server, token))
            } catch (e: Api.Unauthorized) {
                store.clear()
            } catch (e: Exception) {
                // keep the old allowlist; try again on the next SMS
            }
        }
    }

    companion object {
        const val DAY_MS = 24L * 60 * 60 * 1000
    }
}
