package app.hisaab.otp

import android.Manifest
import android.app.Activity
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.text.InputFilter
import android.text.InputType
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView

/** One screen: server address, pairing code, Pair, status, Unpair. */
class MainActivity : Activity() {
    private lateinit var store: Store
    private lateinit var server: EditText
    private lateinit var code: EditText
    private lateinit var status: TextView
    private lateinit var pair: Button
    private lateinit var unpair: Button

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        store = Store(this)
        val d = resources.displayMetrics.density
        val pad = (20 * d).toInt()
        val touch = (48 * d).toInt()
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
        }
        root.addView(TextView(this).apply { text = getString(R.string.app_name); textSize = 22f })
        root.addView(TextView(this).apply {
            text = "Forwards login codes from Zomato, Swiggy, Zepto and Blinkit texts to Hisaab, and only while " +
                "you are connecting an app there. Other texts never leave this phone."
            setPadding(0, pad / 2, 0, pad)
        })
        server = EditText(this).apply {
            hint = "Server address"
            setText(store.server)
            minHeight = touch
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
        }
        code = EditText(this).apply {
            hint = "Pairing code from Hisaab, Accounts"
            minHeight = touch
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_CAP_CHARACTERS or
                InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
            filters = arrayOf(InputFilter.LengthFilter(12), InputFilter.AllCaps())
        }
        pair = Button(this).apply { text = "Pair"; minHeight = touch; setOnClickListener { doPair() } }
        unpair = Button(this).apply { text = "Unpair"; minHeight = touch; setOnClickListener { doUnpair() } }
        status = TextView(this).apply { setPadding(0, pad, 0, 0) }
        listOf(server, code, pair, unpair, status).forEach { root.addView(it) }
        setContentView(root)
        render()
    }

    private fun smsAllowed() = checkSelfPermission(Manifest.permission.RECEIVE_SMS) == PackageManager.PERMISSION_GRANTED

    private fun render(message: String? = null) {
        val paired = store.token != null
        server.isEnabled = !paired
        code.isEnabled = !paired
        pair.isEnabled = !paired
        unpair.isEnabled = paired
        status.text = message ?: when {
            !paired -> "Not paired."
            !smsAllowed() -> "Paired, but SMS permission is off. Allow it in Settings."
            else -> "Paired. Codes are forwarded only while you connect an app in Hisaab."
        }
    }

    private fun doPair() {
        if (!smsAllowed()) {
            requestPermissions(arrayOf(Manifest.permission.RECEIVE_SMS), 1)
            return
        }
        val base = try {
            Api.checkServer(server.text.toString())
        } catch (e: Exception) {
            render("Use an https:// server address.")
            return
        }
        val c = code.text.toString().replace(" ", "").replace("-", "")
        if (c.length != 8) {
            render("The pairing code has 8 characters.")
            return
        }
        pair.isEnabled = false
        render("Pairing...")
        val name = "${Build.MANUFACTURER} ${Build.MODEL}".take(60)
        Thread {
            val msg = try {
                val p = Api.pair(base, c, name)
                store.server = base
                store.savePairing(p.token, p.deviceId, p.senders)
                null
            } catch (e: Api.Failed) {
                if (e.status == 429) "Too many tries. Wait a while." else "That code didn't work. Make a new one in Hisaab."
            } catch (e: Exception) {
                "Couldn't reach the server."
            }
            runOnUiThread {
                code.setText("")
                render(msg)
            }
        }.start()
    }

    private fun doUnpair() {
        store.clear()
        render("Unpaired. Also remove this phone in Hisaab, Accounts, so its token is revoked.")
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED) {
            doPair()
        } else {
            render("Without SMS permission the app can't see OTP texts. You can still type codes by hand.")
        }
    }
}
