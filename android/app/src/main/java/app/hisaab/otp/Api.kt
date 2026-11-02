package app.hisaab.otp

import java.net.HttpURLConnection
import java.net.URL
import org.json.JSONArray
import org.json.JSONObject

/** Tiny HTTPS client. Never logs URLs, request bodies or responses. */
object Api {
    class Unauthorized : Exception()
    class Failed(val status: Int) : Exception()

    /** Plain HTTP only to a local API in debug builds (the network security config enforces it too). */
    fun checkServer(base: String): String {
        val url = URL(base.trim().trimEnd('/'))
        val local = url.host in setOf("localhost", "127.0.0.1", "10.0.2.2")
        require(url.protocol == "https" || (BuildConfig.DEBUG && local && url.protocol == "http")) {
            "Use an https:// address"
        }
        return url.toString()
    }

    private fun call(method: String, url: String, token: String?, body: JSONObject?): String {
        val c = URL(url).openConnection() as HttpURLConnection
        try {
            c.requestMethod = method
            c.connectTimeout = 10_000
            c.readTimeout = 10_000
            c.instanceFollowRedirects = false
            c.setRequestProperty("Accept", "application/json")
            if (token != null) c.setRequestProperty("Authorization", "Bearer $token")
            if (body != null) {
                c.doOutput = true
                c.setRequestProperty("Content-Type", "application/json")
                c.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            }
            val status = c.responseCode
            if (status == 401) throw Unauthorized()
            if (status !in 200..299) throw Failed(status)
            return if (status == 204) "" else c.inputStream.bufferedReader(Charsets.UTF_8).use { it.readText() }
        } finally {
            c.disconnect()
        }
    }

    private fun list(a: JSONArray): List<String> = (0 until a.length()).map { a.getString(it) }

    data class Paired(val token: String, val deviceId: String, val senders: List<String>)

    fun pair(server: String, code: String, name: String): Paired {
        val o = JSONObject(call("POST", "$server/otp-devices/pair", null, JSONObject().put("code", code).put("name", name)))
        return Paired(o.getString("token"), o.getString("device_id"), list(o.getJSONArray("senders")))
    }

    fun config(server: String, token: String): List<String> =
        list(JSONObject(call("GET", "$server/otp-devices/config", token, null)).getJSONArray("senders"))

    fun forward(server: String, token: String, sender: String, body: String, ts: Long) {
        call("POST", "$server/otp-devices/sms", token,
            JSONObject().put("sender", sender).put("body", body).put("ts", ts))
    }
}
