# Hisaab OTP (Android companion, beta)

Forwards Zomato, Swiggy, Zepto and Blinkit login OTPs from SMS to Hisaab while you connect an app, so you don't
have to type them. Package `app.hisaab.otp`, Kotlin, min SDK 26. See `docs/otp-autoforward.md` for the threat model.

Permissions: `INTERNET` and `RECEIVE_SMS` only. Not `READ_SMS`: the app sees a text at the moment it arrives and
never reads the inbox. No foreground service, no boot receiver.

## Build the APK

1. Install Android Studio (Koala or newer; it bundles JDK 17 and the SDK).
2. File > Open > this `android/` folder. Let Gradle sync. (No wrapper jar is committed: Android Studio creates
   one, or run `gradle wrapper` once with Gradle 8.7+.)
3. Optional: set your API address in `app/build.gradle.kts` (`DEFAULT_SERVER`); it can also be typed on the screen.
4. Build > Build App Bundle(s) / APK(s) > Build APK(s). Debug APK: `app/build/outputs/apk/debug/app-debug.apk`.
   For a release APK, Build > Generate Signed App Bundle / APK with your own keystore (never commit it).
5. Unit tests (pure JVM, no device): `./gradlew :app:testDebugUnitTest`. `OtpFilterTest` reads
   `../tests/fixtures/otp_sms.json` and `../server/data/otp_senders.json`, the same vectors the server tests use.

Debug builds may talk plain HTTP to `localhost`, `127.0.0.1` and `10.0.2.2` (emulator host) only. Release builds
are HTTPS only.

## Sideload

1. Copy the APK to the phone (USB, `adb install app-debug.apk`, or a download link).
2. Allow "Install unknown apps" for the app you open it with, then install.
3. Open Hisaab OTP, enter the server address, tap Pair, allow SMS when asked.
4. In the Hisaab PWA: Accounts > Auto-fill OTP on Android > Pair an Android phone. Type the 8-character code into
   the phone within 10 minutes.
5. To stop: Unpair on the phone and Revoke in the PWA (revoking alone also stops it: the next forward gets 401 and
   the phone forgets its token).

## Play Store caveat

Google Play restricts the SMS permission group (including `RECEIVE_SMS`) to apps whose core function needs it and
that pass a Permissions Declaration review; an OTP forwarder for another app's login is unlikely to qualify. This
build is for the beta and is distributed by sideloading only. A Play release would need a different approach
(for example the SMS User Consent API, which asks the user per message).
