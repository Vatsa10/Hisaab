package app.hisaab.otp

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/** Server URL, device token and sender allowlist, encrypted at rest with a Keystore-backed key. */
class Store(context: Context) {
    private val prefs: SharedPreferences = EncryptedSharedPreferences.create(
        context,
        "hisaab_otp",
        MasterKey.Builder(context).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build(),
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
    )

    var server: String
        get() = prefs.getString("server", null) ?: BuildConfig.DEFAULT_SERVER
        set(v) = prefs.edit().putString("server", v).apply()

    val token: String? get() = prefs.getString("token", null)
    val senders: List<String> get() = prefs.getStringSet("senders", null)?.toList() ?: emptyList()
    val configFetchedAt: Long get() = prefs.getLong("config_at", 0L)

    fun savePairing(token: String, deviceId: String, senders: List<String>) {
        prefs.edit().putString("token", token).putString("device_id", deviceId)
            .putStringSet("senders", senders.toSet()).putLong("config_at", System.currentTimeMillis()).apply()
    }

    fun saveSenders(senders: List<String>) {
        prefs.edit().putStringSet("senders", senders.toSet()).putLong("config_at", System.currentTimeMillis()).apply()
    }

    fun clear() {
        prefs.edit().remove("token").remove("device_id").remove("senders").remove("config_at").apply()
    }
}
