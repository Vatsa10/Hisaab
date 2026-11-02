package app.hisaab.otp

/**
 * Pure Kotlin (no Android types) so it runs in JVM unit tests. Mirrors server/otp_devices.py:
 * a sender must fully match an allowlisted pattern (case-insensitive), and the body must carry an OTP keyword and
 * exactly one 4-8 digit number that is not an amount.
 */
class OtpFilter(patterns: List<String>) {
    private val senders: List<Regex> = patterns.mapNotNull {
        try {
            Regex(it, RegexOption.IGNORE_CASE)
        } catch (e: IllegalArgumentException) {
            null
        }
    }

    fun senderAllowed(sender: String?): Boolean {
        val s = sender?.trim() ?: return false
        return senders.any { it.matches(s) }
    }

    /** True only for an allowlisted sender whose text holds exactly one code. Everything else is dropped. */
    fun shouldForward(sender: String?, body: String?): Boolean = senderAllowed(sender) && extractCode(body) != null

    companion object {
        private val KEYWORD =
            Regex("""\b(?:otp|code|verification|one[\s-]?time[\s-]?password)\b""", RegexOption.IGNORE_CASE)
        private val NUMBER = Regex("""(?<![\d,.])\d{4,8}(?![\d,])""")
        private val CURRENCY_BEFORE = Regex("""(?:rs\.?|inr|₹)\s*$""", RegexOption.IGNORE_CASE)

        fun extractCode(body: String?): String? {
            if (body.isNullOrEmpty() || !KEYWORD.containsMatchIn(body)) return null
            val found = NUMBER.findAll(body)
                .filter { !CURRENCY_BEFORE.containsMatchIn(body.substring(0, it.range.first)) }
                .map { it.value }
                .toSet()
            return if (found.size == 1) found.first() else null
        }
    }
}
