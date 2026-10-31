# End-to-end test on this machine

One command runs the whole product locally and drives it like an iPhone 14 user would:

```
PWA (next start) -> API (uvicorn) -> jobs queue -> worker (fake apps) -> Postgres -> Realtime -> PWA
```

```bash
bash scripts/e2e_local.sh
```

It ends with one line, for example `E2E OK: 26 passed in 6m1s`, or `E2E FAILED: ...` with where to look.

Prerequisites: Docker Desktop running (it also runs Mailpit; ports 1025 and 8025 free), Python 3.11+ with `requirements.txt` installed, Node 22, pnpm 10, curl.
Git Bash on Windows 11, or any shell on Linux/macOS. The first run downloads Playwright's WebKit (on Linux also
run `pnpm --dir e2e exec playwright install --with-deps webkit` once for its system libraries).

## What the script does

1. **Supabase.** `scripts/dev_supabase.sh` starts local Supabase if it is down (safe to repeat) and writes
   `web/.env.local` and `.env.server.local`.
2. **PWA build.** `next build` in `web/` with the three public values: the Supabase URL and anon key from
   `web/.env.local` and `NEXT_PUBLIC_API_URL=http://localhost:8010`. `web/node_modules` is installed if missing.
3. **Clean database.** `npx supabase db reset` re-applies every migration, which also removes the users earlier
   runs created. The CLI's reset sometimes fails while "Initialising schema" (`DbSetupError`: the realtime
   container races its own `schema_migrations` insert), so the script tries up to three times. Then
   `dev_supabase.sh` runs again to give the `ask_reader` login a fresh password.
4. **Mailpit and the admin.** `docker run axllent/mailpit:v1.21` (SMTP 127.0.0.1:1025, API and UI 127.0.0.1:8025)
   catches every email of the run; `SMTP_HOST`, `SMTP_PORT` and `APP_URL` are exported to the API, the worker and
   the specs. Sign-up is invite-only, so `python cli.py add-admin admin@e2e.test` runs right after the reset, and
   `E2E_ADMIN_EMAIL`, `E2E_PY`, `DATABASE_URL`, `E2E_SUPABASE_URL`, `E2E_ANON_KEY` and `E2E_API_URL` are exported
   to Playwright (`e2e/helpers/stack.ts` uses them to invite, run `server.scheduler --once`, query the database
   and call the API as a user; `e2e/helpers/mailpit.ts` reads the mailbox).
5. **Services**, each with `.env.server.local` plus its own switches:

   | Service | Command | Port | Switches |
   | --- | --- | --- | --- |
   | API | `python -m uvicorn server.api:app` | 8010 | `ASK_FAKE=1`, `CORS_ORIGINS=http://localhost:3110` |
   | Worker | `python -m server.worker`, started by `e2e/.run/start-worker.sh` | - | `WORKER_FAKE=1`, `CATEGORIZE=0`, `FAKE_PAGE_DELAY_S=0.4`, `PROFILE_ROOT=e2e/.run/profiles`, `DRAIN_S=5`, `MATCH_JUDGE=0` |
   | PWA | `next start` | 3110 | - |

   The script waits for `/healthz`, the worker's `worker started` line and the PWA's `/login` before going on.
   The worker is started through one launcher file (`e2e/.run/start-worker.sh`, with its PID in `e2e/.run/worker.pid`
   and `E2E_RUN_DIR` exported), so a spec can stop it and start it again with extra switches: `restartWorker(env)` in
   `e2e/helpers/stack.ts`. The cleanup trap stops a worker a spec restarted too.
6. **Playwright** (`e2e/`, its own `package.json` with `@playwright/test` pinned, so `web/` keeps no test
   browser dependency) runs `e2e/tests/product.spec.ts`, `e2e/tests/plan4.spec.ts` and `e2e/tests/plan5.spec.ts` on the iPhone 14 profile
   (WebKit, 390x844, touch).
7. **Cleanup.** A trap stops the API, the worker and the PWA (with their child processes; `taskkill /T` on
   Windows) however the run ends, removes the Mailpit container, and stops Supabase if this run started it. Set `E2E_KEEP_SUPABASE=1` to keep it.

Extra arguments go to Playwright: `bash scripts/e2e_local.sh -g "updates live"`, or `bash scripts/e2e_local.sh tests/plan4.spec.ts`.
`E2E_SKIP_BUILD=1` reuses an earlier `web/.next` from this script instead of building again.

**Known gap, worked around by the script.** The Supabase CLI skips `supabase/migrations/0008_cheapest_cart.sql (end of file)`
(its name does not match `<timestamp>_name.sql`), so after `db reset` the `app_addresses` table is missing. The script
detects that, applies the file by hand, and restarts the Realtime container (it does not see a table added to its
publication while running). Rename the migration and this step becomes a no-op.

### Host names

The browser opens the PWA at `localhost`, and the PWA calls the API at `localhost`. Supabase Auth only
redirects to the host of its `site_url` and allow-list (`http://localhost:3000` in `supabase/config.toml`), so
serving the PWA on `127.0.0.1` would break sign-up and OAuth redirects. The port can differ; the host cannot.

Supabase itself stays at `127.0.0.1`, as `supabase status` and `dev_supabase.sh` give it. Do not change it to
`localhost`. On Windows with WSL, `wslrelay.exe` also listens on `[::1]` for every port Docker publishes
(54321, 54322, …), and sometimes accepts a connection without ever answering it. Browsers and Node try `::1`
first for `localhost`, so a `localhost` Supabase URL stalls sign-ups, server renders and Realtime at random.
You can see it with `netstat -ano | grep 54321`: one listener is `com.docker.backend.exe`, the other on `[::1]`
is `wslrelay.exe`.

### The fakes

- `WORKER_FAKE=1` (`server/fake_sites.py`): no browser, no network. The OTP is `123456`; any other code fails the
  login (`login_failed`). A phone ending in `0000` is first asked "Which city do you order from?". Every connect
  finds 28 orders over six months, every 11th cancelled; Swiggy's every third order is an Instamart order. The
  orders arrive in pages of 8 and each page reports a live "Found so far" count. `FAKE_PAGE_DELAY_S=0.4`
  pauses between pages, so that count stays on screen long enough to check.
- `ASK_FAKE=1` (`server/api.py`): `/ask` runs one fixed query (spend per app from `monthly_spend`) through the
  same `scoped_sql` guard and row-level security as the real analyst, and answers with a markdown table. No LLM.
- `CATEGORIZE=0`: no OpenAI call after a sync.

Both fakes refuse to start unless `DATABASE_URL` points at this machine.

## What the specs cover

### `e2e/tests/product.spec.ts`

Each test invites its own address (`cli.py invite`) and signs it up (local Supabase has email confirmation off).

1. **Sign up, connect Swiggy, Spend, Orders and Ask fill in**
   - Spend before any app is connected is mockup A at zero: the month eyebrow, "This month so far ₹0", four apps
     at ₹0, no bars, "Connect an app".
   - Accounts lists Zepto, Blinkit, Swiggy and Zomato; there is no Instamart row, only the note that it comes
     with Swiggy.
   - Consent ("I understand"), phone, "Send OTP"; the row shows "Syncing…"; the job reaches `otp_needed` and the
     OTP field appears. The field has `inputmode="numeric"`, `autocomplete="one-time-code"`, `maxlength="8"`.
   - The OTP `123456` is typed digit by digit, and the field sends itself.
   - The first sync is pushed over Realtime. A live "Found so far" count must appear, never decrease, and stay
     within the final count. The final "Found" card shows 28 orders, an amount, and the first month. The phone number and OTP are in no
     browser storage or cookie.
   - Spend has six bars with non-zero totals that add up to no more than the sync found. The big number is the
     current month's bar, and each month's Swiggy row matches its bar. Instamart shows as a sub-label of Swiggy
     in at least one month and never as its own row.
   - Orders lists all 28, with two "Cancelled". Instamart orders are titled "Instamart" with "Swiggy · date". An
     Instamart order's detail shows the Swiggy eyebrow, total, order ID and items.
   - Ask answers with a table (`site`, `amount`) holding exactly one row, `swiggy`, equal to the sync total.
   - Signing out and back in keeps everything.
2. **A login that asks a question first.** Zomato with a phone ending `0000`: the question appears as the field's
   label, the answer is sent, then the OTP, then the first sync.
3. **A wrong OTP.** Zepto with `000000` shows "That code didn't work". "Try again" starts a new login, and `123456`
   then connects.
4. **Connect flow updates live.** A second iPhone context, signed in as the same user, waits on Accounts until
   its Realtime channel is subscribed to Postgres changes. It then sees Blinkit go to "Syncing…" while the first
   phone waits for the OTP, and to "✓ Synced" after it, without reloading.

No horizontal scroll: every tab (Spend, Orders, Cart, Ask, Accounts) is checked at 390 px and at 375 px, empty
and with data. So are an order's detail page and Ask with a table on screen. Nothing on the page may be wider
than the screen, and a sideways scroll must not move it. The 375 px checks run in a separate 375 px context
that shares the signed-in session. They never resize the page: resizing a mobile WebKit page in place leaves its
layout viewport at the old height, and the fixed tab bar becomes unclickable.

### `e2e/tests/plan4.spec.ts`

Serial; the tests hand state to each other. Emails are read from Mailpit; the queued ones are sent by the worker
after a job or by `python -m server.scheduler --once` (`schedulerOnce`).

1. **Invite-only sign-up joins the waitlist.** An uninvited address gets the invite-only message, no account, and a
   waitlist row.
2. **Admin invites from the waitlist.** The admin (`admin@e2e.test`) signs up, `/admin` -> Invites -> Invite on the
   waitlist row; the mail "You're invited to Hisaab" arrives; that address then signs up.
3. **First sync sends an email.** Connect fake Zepto with `123456`; the mail "Zepto is connected" arrives.
4. **Refresh limit.** "Refresh now · 3 left today", three refreshes each waited to done, then "No refreshes left
   today"; a direct fourth `POST /connections/zepto/refresh` answers 429 with the exact detail.
5. **Admin sees cost per user.** The member's row has worker minutes above zero, a rupee amount and four syncs.
6. **Admin learn with approval.** The admin connects fake Zepto; Sites -> Learn shows the approval card with
   `^https://fake\.zepto\.test/api/orders$`; Approve; Sites shows "Recipe v1" and Healthy. The grocery-price recipe
   is then learned through the API so the next test can compare.
7. **A second sync and a compare ask for no login.** After one connect (the only OTP), Refresh now and a cart
   compare both finish with no OTP or question stage, no second connect job and the connection still connected.
   `fake_sites` jobs bypass the vault, so the vault itself is exercised by a save and restore round trip of the same
   `vault.save_profile` / `restore_profile` on a throwaway file vault (encrypted blob, owner-checked restore).
8. **Deleting the account.** Accounts -> Delete account -> DELETE: the page leaves for `/login?deleted=1`, the
   `auth.users` row is gone, signing in at Auth and in the app fails, every row of the user is gone, and, as the
   role of `DATABASE_URL` (the API's local role), `SELECT ... FOR KEY SHARE` and `FOR UPDATE` on an `auth.users`
   row both work.
9. **Budget pause.** `schedulerOnce({MONTHLY_BUDGET_INR: "0.01"})` -> "Budget reached" mail to the admin and
   `budget_state` paused; a freshly invited address sees "Sign-ups are paused"; `schedulerOnce({})` restores `ok` and
   the same address then signs up.
10. **No horizontal scroll on /admin**, on each of its five tabs.

### `e2e/tests/plan5.spec.ts`

Serial; the tests hand state to each other, and it also runs on its own (`bash scripts/e2e_local.sh tests/plan5.spec.ts`).
The delivery place is "12 Shanti Society, Gorwa, Vadodara, Gujarat" (22.3300, 73.1500), a made-up address. The fake apps'
own saved addresses are in Bengaluru, so `setAddress(email, address, lat, lon)` (in `stack.ts`) writes the place into
the profile and gives every app the user has connected one saved address at that pin; call it after the connects,
because a later sync of an app puts that app's own saved addresses back. `setConnectionStatus(email, site, status)`
flips a connection (for example to `needs_login`).

1. **Admin learns price recipes.** The admin connects fake Zepto, Blinkit, Swiggy and Zomato, then Sites -> Learn for
   Zepto, Blinkit and Instamart grocery prices and Swiggy and Zomato restaurants and menus. Instamart's and Zomato's
   POST search ask for the admin's Approve (`^https://fake\.instamart\.test/api/search$`,
   `^https://fake\.zomato\.test/api/search$`). Every card ends "Recipe v<n>" and Healthy. On a database an earlier run
   already approved, the POST is on the allowlist and no question comes: the test takes either.
2. **Connect prompts for unconnected apps.** A user with only Zepto sees Blinkit and Instamart as "Connect" on `/cart`;
   after a compare the Blinkit card reads "Connect Blinkit to compare." with a Connect link to `/accounts`, and Instamart
   "Connect Swiggy to compare Instamart.". With Zepto flipped to `needs_login`, Compare is disabled with "Connect
   Blinkit, Zepto or Instamart to compare." and the Zepto row says "Log in again".
3. **Usual items.** All four apps connected: the Accounts address list shows the fake saved addresses, and "Start from
   my usual" lists items with "×<N> in 90 days", N at least 3.
4. **Cheapest cart.** The five-item list: header "5 items · priced live at Gorwa, Vadodara" and the price note; Zepto
   "₹422" with the ring and "Cheapest"; Blinkit "₹451 +₹29"; Instamart "₹458" with "1 to pick"; "fees estimated";
   "Fill cart on Zepto" shown and disabled (no cart recipe is learned in this run, so the server says it is not
   available for Zepto yet).
5. **Item table and split line.** Totals row "₹422 ₹451 ₹458" and "No item is cheaper on another app, so one order
   on Zepto is best.".
6. **Pick one.** Edit list -> "milk" and "Aashirvaad Atta 5kg": every card shows "1 to pick"; on Zepto "Amul Taaza
   Toned Milk 1 L" gives "₹309", ranked first, and `/me/limits` `cart_checks_left` is unchanged.
7. **Food compare.** "Restaurants you order from" -> Meghana Foods -> a pair card with both app dots; one Chicken
   Biryani (Half) x2 and a Paneer Biryani; Swiggy "₹836" Cheapest with the fee line, Zomato "₹878 +₹42", the offers
   note, "Fill cart on Swiggy" disabled, Paneer Biryani highlighted on Zomato, no split line, and exactly one check used.
8. **Restaurant pairing.** Behrouz (a Gorwa pair and a separate Alkapuri card "Only on Zomato"), Truffles ("Check this is
   the same restaurant" -> both listings -> "Yes, same restaurant"), Burger Singh ("Closed on Zomato right now."),
   Theobroma ("Only on Swiggy"). The test waits for each menu before searching again: the server takes one lookup at a time.
9. **Logged out at compare time.** `restartWorker({FAKE_LOGGED_OUT_SITES: "blinkit"})`: the Blinkit card reads
   "Blinkit logged you out. Log in again to compare.", Mailpit gets "Blinkit logged you out", Accounts shows Blinkit
   "Needs login"; the worker is then restarted without the switch.
10. **Daily limit.** Checks through the API until the 429 "You can check 10 carts a day. Try again tomorrow."; `/cart`
    shows "0 checks left today" and Compare is disabled.
11. **Budget pause.** A third user (Swiggy, so Instamart and food both have an app) with `schedulerOnce({MONTHLY_BUDGET_INR:
    "0.01"})`: Compare and a restaurant search both show "Cart checks are paused until 1 <month>."; `schedulerOnce({})`
    restores it.
12. **No horizontal scroll** on `/cart` (both modes) and `/cart/[id]` (both modes, item table open).

Nothing sleeps: every wait is for text that the API or Realtime puts on screen (Playwright's auto-waiting
`expect`, 30 s default, 90 s for a sync).

## Results and logs

| Path | What |
| --- | --- |
| `e2e/playwright-report/` | HTML report (`pnpm --dir e2e exec playwright show-report playwright-report`) |
| `e2e/test-results/` | Traces and screenshots of failed tests |
| `e2e/.run/logs/` | `api.log`, `worker.log`, `web.log`, `web-build.log`, `supabase.log`, `mailpit.log`, `add-admin.log`, `playwright.log` |

All are gitignored. The script never prints a secret:
- `dev_supabase.sh`'s stdout, which names the anon key, goes to `/dev/null`.
- Everything the Supabase CLI writes passes through a redaction filter before it reaches `supabase.log` or the
  terminal. `supabase start` prints the anon and service-role keys, the JWT secret and the S3 keys.
- The services log no tokens, phone numbers, OTPs or answers.

## Troubleshooting

- **`port 8010 is already in use`** (or 3110): another API or PWA is running. The ports are fixed because the PWA
  build bakes the API URL in and the API's CORS allows only `http://localhost:3110`.
- **A job never leaves "Opening …"**: another worker is connected to the same local database and claimed it.
  Stop it. Only the script's fake worker may run during the test.
- **Mailpit does not start**: ports 1025 or 8025 are taken (another Mailpit, a local mail catcher). The script removes
  a leftover `ea-e2e-mailpit` container itself.
- **`next build` collides with `pnpm dev`**: stop the dev server first. Both use `web/.next`.
- **Run the spec against a stack you started yourself**:
  `E2E_BASE_URL=http://localhost:3110 pnpm --dir e2e exec playwright test`.
