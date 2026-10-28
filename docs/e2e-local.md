# End-to-end test on this machine

One command runs the whole product locally and drives it like an iPhone 14 user would:

```
PWA (next start) -> API (uvicorn) -> jobs queue -> worker (fake apps) -> Postgres -> Realtime -> PWA
```

```bash
bash scripts/e2e_local.sh
```

It ends with one line, for example `E2E OK: 4 passed in 4m12s`, or `E2E FAILED: ...` with where to look.

Prerequisites: Docker Desktop running, Python 3.11+ with `requirements.txt` installed, Node 22, pnpm 10, curl.
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
4. **Services**, each with `.env.server.local` plus its own switches:

   | Service | Command | Port | Switches |
   | --- | --- | --- | --- |
   | API | `python -m uvicorn server.api:app` | 8010 | `ASK_FAKE=1`, `CORS_ORIGINS=http://localhost:3110` |
   | Worker | `python -m server.worker` | - | `WORKER_FAKE=1`, `CATEGORIZE=0`, `FAKE_PAGE_DELAY_S=0.4`, `PROFILE_ROOT=e2e/.run/profiles`, `DRAIN_S=5` |
   | PWA | `next start` | 3110 | - |

   The script waits for `/healthz`, the worker's `worker started` line and the PWA's `/login` before going on.
5. **Playwright** (`e2e/`, its own `package.json` with `@playwright/test` pinned, so `web/` keeps no test
   browser dependency) runs `e2e/tests/product.spec.ts` on the iPhone 14 profile (WebKit, 390x844, touch).
6. **Cleanup.** A trap stops the API, the worker and the PWA (with their child processes; `taskkill /T` on
   Windows) however the run ends, and stops Supabase if this run started it. Set `E2E_KEEP_SUPABASE=1` to keep it.

Extra arguments go to Playwright: `bash scripts/e2e_local.sh -g "updates live"`.

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

## What the spec covers

`e2e/tests/product.spec.ts`. Each test signs up its own user (local Supabase has email confirmation off).

1. **Sign up, connect Swiggy, Spend, Orders and Ask fill in**
   - Spend before any app is connected is mockup A at zero: the month eyebrow, "This month so far ₹0", four apps
     at ₹0, no bars, "Connect an app".
   - Accounts lists Zepto, Blinkit, Swiggy and Zomato; there is no Instamart row, only the note that it comes
     with Swiggy.
   - Consent ("I understand"), phone, "Send OTP"; the row shows "Syncing…"; the job reaches `otp_needed` and the
     OTP field appears. The field has `inputmode="numeric"`, `autocomplete="one-time-code"`, `maxlength="6"`.
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

Nothing sleeps: every wait is for text that the API or Realtime puts on screen (Playwright's auto-waiting
`expect`, 30 s default, 90 s for a sync).

## Results and logs

| Path | What |
| --- | --- |
| `e2e/playwright-report/` | HTML report (`pnpm --dir e2e exec playwright show-report playwright-report`) |
| `e2e/test-results/` | Traces and screenshots of failed tests |
| `e2e/.run/logs/` | `api.log`, `worker.log`, `web.log`, `web-build.log`, `supabase.log`, `playwright.log` |

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
- **`next build` collides with `pnpm dev`**: stop the dev server first. Both use `web/.next`.
- **Run the spec against a stack you started yourself**:
  `E2E_BASE_URL=http://localhost:3110 pnpm --dir e2e exec playwright test`.
