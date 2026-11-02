# OTP auto-forward (Android companion)

Connecting Zomato, Swiggy, Zepto or Blinkit makes the worker log in, and the app sends the user an OTP by SMS. A web
page cannot read SMS, so the PWA asks the user to type it (`POST /jobs/{id}/otp`). The optional Android app
`android/` forwards that SMS for them. Written from scratch for Hisaab.

## Flow

1. PWA, Accounts, "Auto-fill OTP on Android": `POST /otp-devices/pairing` returns an 8-character code
   (alphabet without 0/O/1/I/L), valid 10 minutes, single use. Only its sha256 is stored. 5 codes per user per hour.
2. Phone: `POST /otp-devices/pair {code, name}` (no user token; 10 tries per IP per hour) returns
   `{token, device_id, senders}`. The token (32 random bytes) is shown once and stored on the phone in
   EncryptedSharedPreferences; the server keeps only its sha256.
3. When an SMS arrives, the phone drops it unless the sender matches the allowlist and the text holds exactly one
   code. Otherwise it sends `POST /otp-devices/sms {sender, body, ts}` with the device token.
4. The server checks again: device token live (not revoked), 10 SMS per device per minute, `ts` (epoch ms) no older
   than 2 minutes and no more than 1 minute ahead, sender on the allowlist, an OTP keyword (otp, code, verification,
   one time password), exactly one 4-8 digit number that is not an amount. Then it NOTIFYs the code on `otp_<job>`
   (the channel `POST /jobs/{id}/otp` uses) for each of that user's own jobs that are running, of type `connect` or
   `fill_cart`, at stage `otp_needed`, on the sender's app (Swiggy also covers Instamart). Every outcome answers
   204 with no body.
5. `GET /otp-devices` lists devices (never tokens). `DELETE /otp-devices/{id}` revokes; the next request with that
   token gets 401 and the phone wipes its token.

## Threat model

| Threat | Mitigation |
| --- | --- |
| Phone app leaks unrelated texts (bank OTPs, personal) | Sender allowlist and code check on the phone; non-matching texts never leave it. RECEIVE_SMS only, no inbox access. |
| Malicious or buggy phone pushes a code into someone else's login | Device token is bound to one user; jobs are selected by that user_id only. Tested. |
| Code relayed into the wrong app's login | Brand from sender must match the job's site. |
| Replay of an old SMS | ts window (2 min back, 1 min ahead) and relay only while a job is waiting for an OTP. |
| Probing the endpoint | Always 204, empty body; rate limits on pairing, pair and SMS. |
| Brute-forcing pairing codes | 31^8 codes, 10 minute life, single use, 10 tries per IP per hour, hash lookup plus constant-time compare. |
| Stolen device token | Revocable from the PWA, effective on the next request; hash only on the server; never in backups (allowBackup=false). |
| Database leak | Only sha256 of tokens and codes; no SMS content anywhere. |
| Logs | Only counts (`relayed=N`) and exception class names. A test captures logs and checks no code, body or sender appears. |
| Account deletion | `otp_devices` and `otp_pairings` have `user_id`, so `server/erase.py` finds them from the catalog. Tested. |

Known limits: rate limits for `/pair` and `/sms` are in-process (reset on API restart; fine for one API process).
A lookalike or spoofed sender ID that matches the pattern could forward a code, but only into the user's own
waiting job for that app, which is no worse than the user typing a wrong code.

## What is collected

- Stored: device name (phone model), created, last seen, revoked timestamps; hashes of tokens and pairing codes.
- Not stored or logged, ever: SMS body, sender, the code. The code exists only in the NOTIFY payload to the worker.
- On the phone: server address, device token, sender patterns (all encrypted).

## Sender IDs: confirm during the demo

`server/data/otp_senders.json` holds best-guess patterns such as `VK-ZOMATO`, `AD-SWIGGY`, `VM-ZEPTON`, `BP-BLNKIT`
(optional two-letter TRAI prefix, optional -S/-T/-P/-G suffix, case-insensitive). These are not verified. During the
demo, read the real sender ID of each app's OTP SMS and fix the file; the phone picks changes up at the next pairing
or daily refresh.

## Demo steps

1. Build and install the APK (`android/README.md`). For a local API use a debug build and `adb reverse tcp:8000
   tcp:8000`, server `http://localhost:8000`.
2. PWA, Accounts, Pair an Android phone. Enter the code on the phone, tap Pair, allow SMS.
3. Connect Zomato in the PWA with the phone's number. When the OTP SMS arrives the connect step continues without
   typing. If it doesn't, check the sender ID (above) and type the code by hand: manual entry always works.
4. Revoke the phone in the PWA; the next SMS gets 401 and the phone shows Not paired.

## iPhone

iOS gives apps no access to SMS. iPhone users tap the code Apple suggests above the keyboard (Security Code AutoFill)
in the PWA's OTP field, which needs `autocomplete="one-time-code"` on that input.
