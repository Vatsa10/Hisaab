package app.hisaab.otp

import java.io.File
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Same vectors as tests/server/test_otp_devices.py, read from the repo's tests/fixtures/otp_sms.json. */
class OtpFilterTest {
    private fun repoFile(rel: String): File {
        var dir: File? = File("").absoluteFile
        while (dir != null && !File(dir, rel).exists()) dir = dir.parentFile
        return File(dir ?: error("cannot find $rel above the working directory"), rel)
    }

    private val senders: Map<String, List<String>> =
        JSONObject(repoFile("server/data/otp_senders.json").readText()).let { o ->
            o.keySet().filterNot { it.startsWith("_") }.associateWith { k ->
                val a = o.getJSONArray(k)
                (0 until a.length()).map { a.getString(it) }
            }
        }

    private val all = OtpFilter(senders.values.flatten())

    @Test
    fun fixtureVectors() {
        val cases = JSONObject(repoFile("tests/fixtures/otp_sms.json").readText()).getJSONArray("cases")
        assertTrue(cases.length() > 10)
        for (i in 0 until cases.length()) {
            val c = cases.getJSONObject(i)
            val id = c.getString("id")
            val sender = c.getString("sender")
            val body = c.getString("body")
            val brand = if (c.isNull("brand")) null else c.getString("brand")
            val code = if (c.isNull("code")) null else c.getString("code")
            val gotBrand = senders.entries.firstOrNull { (_, ps) -> OtpFilter(ps).senderAllowed(sender) }?.key
            assertEquals(id, brand, gotBrand)
            assertEquals(id, code, if (gotBrand != null) OtpFilter.extractCode(body) else null)
            assertEquals(id, code != null, all.shouldForward(sender, body))
        }
    }

    @Test
    fun emptyAllowlistForwardsNothing() {
        assertFalse(OtpFilter(emptyList()).shouldForward("VM-ZOMATO", "582913 is your OTP"))
    }

    @Test
    fun badPatternIsIgnored() {
        assertTrue(OtpFilter(listOf("(", "^ZOMATO$")).senderAllowed("zomato"))
    }
}
