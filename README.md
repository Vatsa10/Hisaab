# Hisaab

**हिसाब — your food and grocery spending, sorted. Built for India.**

Connect Blinkit, Zepto, Swiggy (incl. Instamart) and Zomato once with your phone number and OTP, and see
every order in one place: per month, per app, per restaurant and per category. Ask questions about your
spending in plain words. Before you shop, get your grocery list or your usual restaurant order priced live
on each app at your address, with fees, so you know which one is cheapest.

- **Read-only by design.** Hisaab reads order history and prices. It never places, changes or cancels an
  order, and never touches payment or your saved addresses. Filling a cart, when you ask for it, only adds
  items; you pay in the app yourself.
- **No hand-written scrapers.** An AI agent learns each app's order and search APIs once (admin-supervised)
  and saves them as JSON "recipes". Everyday syncs just replay them: fast, cheap and repeatable.
- **Your data stays yours.** Row-level security in Postgres, encrypted app sessions, OTPs never stored,
  phone numbers and tokens never logged.

### Status

| Part | State |
|---|---|
| CLI (single user, SQLite) | Working — everything below this section |
| Cloud worker, API, job queue (Postgres) | Built and tested locally |
| iPhone PWA: sign-in, connect apps, Spend, Orders, Ask | Built; local end-to-end test in progress |
| Daily sync, limits, budget, admin, notifications | Planned (`docs/superpowers/plans/`) |
| Cheapest cart (groceries and food) and fill cart | Planned |
| Hardening, then deploy (Vercel + Azure + Supabase) | Planned |

Stack: Next.js PWA (Vercel), FastAPI and a Chrome worker on Azure Container Apps, Supabase (Auth, Postgres,
Realtime), Playwright, Claude Haiku 5.5 and TypeSafe Jev. Local development: [docs/dev-supabase.md](docs/dev-supabase.md)
and [web/README.md](web/README.md).

---

## The CLI (single user)

The original tool, still fully supported. It is a local, read-only agent that pulls your own order history
from Blinkit, Zepto, Swiggy (incl. Instamart) and Zomato into SQLite, categorizes items, and reports spend.

There are no per-site scrapers in the code. An LLM agent finds each site's order-history API at
runtime and saves what it learns as a JSON "recipe" in `recipes/<site>.json`.

## Safety model

- Your installed Google Chrome, one persistent profile per site (`profiles/<site>`). Swiggy and Instamart
  share one profile. Chrome is started as a normal browser with a local debugging port, and Playwright
  attaches over CDP. It isn't launched in automation mode (no "controlled by automated test software"
  bar), which is what made Swiggy return 403.
- Every request goes through `guard.py`:
  - GET, HEAD and OPTIONS are allowed.
  - POST is allowed only in login mode, or when it matches the recipe's `read_post_allowlist` and the
    body doesn't contain `mutation`.
  - The one exception: POSTs to bot-challenge services (`*.awswaf.com`, Cloudflare's
    `/cdn-cgi/challenge-platform/`) are always allowed. They go to the WAF vendor, not your account, and
    blocking them leaves Swiggy on a blank "Max challenge attempts exceeded" page.
  - Every other method is aborted and logged.
- Navigations matching `cart|checkout|payment|pay|address|place-?order|cancel` are aborted.
- Downloads are denied through CDP `Browser.setDownloadBehavior: deny`, for the whole browser and for each
  context. If that call fails, the session doesn't start.
- The discovery agent has no typing tool. It refuses to click submit buttons, form buttons, and elements
  whose text looks like a write action (add, cart, pay, cancel, reorder, invoice, logout, …).
- The login agent can type only two things: your phone number and an OTP that you enter in the terminal.
  The LLM picks the input field and names the value (`PHONE` or `OTP`); it never sees the number or the
  code. Both are hidden from every page snapshot and log. The number is kept in memory and never written
  to disk.
- Before the agent adds a POST endpoint to the allowlist, it shows you the entry in the terminal and asks
  for `y`.
- Cookies and auth/token headers are never captured, saved or logged. Logged URLs have their query strings
  removed.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env        # set ANTHROPIC_API_KEY, optionally TYPESAFE_API_KEY
```

Every LLM call goes through `llm.py` on Claude Haiku 5.5 (`ANTHROPIC_MODEL=claude-haiku-5-5`, the default).
Cost policy: no model at or above $1 per million tokens, input or output, and no prompt over 90K tokens, so
Haiku's price never crosses its 100K-token step. Any other model, or a larger prompt, is refused at start-up
or before the call; there is no override.

Google Chrome has to be installed. It's found in the usual install folders; otherwise set `CHROME_PATH`
in `.env`. Close any Chrome window left open on a site's profile from an earlier run before starting a new
one. Your everyday Chrome doesn't matter, because it uses a different profile.

## Usage

Sites: `blinkit`, `zepto`, `swiggy`, `instamart`, `zomato`.

```bash
python cli.py login             # asks for your number once, then logs in to Blinkit, Zepto, Swiggy and
                                # Zomato in turn, asking for each site's OTP
python cli.py login zepto       # one site
python cli.py login --manual    # no auto-fill: log in by hand, open your Orders page, press Enter
python cli.py login --dry-run   # test all 4: stops once the phone input is reached, no OTP sent
python cli.py learn blinkit     # LLM agent finds the order API and writes recipes/blinkit.json
python cli.py sync blinkit      # fetch everything
python cli.py sync all --since 2025-01-01   # sites run in parallel, one Chrome each (--workers 4)
python cli.py categorize        # LLM item categories, cached by item name
python cli.py report --month 2025-06   # prints tables + reports/report_2025-06.csv
python cli.py months --last 6          # month-wise split by platform and by category (+ CSVs)
python cli.py insights                 # analyst agent: month-wise breakdown, trends, top restaurants
python cli.py ask "how much did I spend on pizza per month in 2026?"
```

Start with one site (Blinkit), get `login`, `learn` and `sync` working end to end, then add the others.

The first time you run `login`, `learn` or `sync`, it asks for your delivery address as
`society/building, area, city, state` (for example `Sunrise Heights, Alkapuri, Vadodara, Gujarat`), or
`lat,lon` copied from Google Maps. A bare `City, State` is rejected because it lands on the city centre.
The address is looked up once through OpenStreetMap; if the society isn't known there, the lookup drops
the society, then the area, until it finds a match. The result is saved in `settings.json`. Every browser
session shares those coordinates with geolocation permission already granted, so "Use my current
location" works. If a site needs the address typed, the login agent types the full address into its
search box. Change it with `python cli.py location`.

When the OTP field appears, the code notices it (not the model) and asks you for the code in the
terminal. It handles both a single OTP input and split one-digit boxes.

### How the browser agent picks each step

1. **Snapshot.** It lists only the elements you could actually click right now: the centre of each one
   must hit the element itself. When a popup is open, only the popup's contents are listed.
2. **Code checks.** It checks for an OTP field, or a logged-out page with a Login button.
3. **Jev decides.** One TypeSafe call (`kind` and `item`, about 0.5 s) picks the next action. If
   `TYPESAFE_API_KEY` isn't set, Jev is unsure (confidence below 0.4), or the last action failed or
   repeated, a single Claude tool call decides that step instead.
4. **Act.** It clicks normally, falls back to a real mouse click at the element's centre, then waits
   until the page stops changing instead of using fixed sleeps.
5. **No early finish.** It won't accept "done" while a Login button is visible, or while the OTP is still
   missing. During `learn`, it won't accept "done" until a response with real order records has arrived for
   a further page. Before refusing, code clicks a visible "Load More", "Next" or next page number ("2")
   itself, just as it fills the OTP itself. A separate Jev yes/no check ("does this page show the user's
   past orders?") must also agree. Each refusal can happen at most twice, so an unsure check can't block
   for ever.
6. **Ask, don't fail.** When it's stuck (a CAPTCHA, a sign-up form, an unclear page), it asks you a question
   in the terminal. You can answer, fix it in the browser and press Enter, or type `stop`.

Other details:
- The snapshot also covers same-site iframes (Zomato's login form is one).
- Links that open a new tab are followed.
- Icon-only buttons take their label from their image or SVG.
- Safe actions (scroll, wait, Escape) need less Jev confidence (0.3) than clicks and typing (0.4).
- Every step is logged to `runs/<site>-<login|learn>-<time>.jsonl`: the decision, Jev's probabilities,
  timings and progress, with the phone number and OTP masked. Use it to see why the agent did something.

If you run `sync` before logging in, it notices that you're logged out and asks whether to log in now.

**Instamart** isn't synced on its own. On swiggy.com, Instamart's "My Account" opens the same single order
feed as Swiggy food (`/dapi/order/all`), and there's no separate web order list. So `sync swiggy` already
stores everything the web shows, and a separate Instamart recipe could only duplicate Swiggy orders and
mislabel them. `sync instamart` prints this explanation. Two safeguards in `learn` back this up: sites that
share a database site may not share an order endpoint, and each site's own goal is passed to the agent.

### How sync works

1. **Fast path.** `sync` opens `orders_page_url` and calls the learned endpoint from inside the page with
   `fetch(...)`, so the site's own cookies apply. It then follows the pagination with 1–2 s pauses until
   it reaches `--since` or runs out of pages.
2. **Fallback 1.** If the fast path fails, `sync` replays the recipe's `nav_trace` (clicks, scrolls and URL
   checkpoints) and reads the order API responses the page loads.
3. **Fallback 2.** If that also fails, `sync` logs `DRIFT`, runs discovery again, and overwrites the recipe.
4. If the session has expired (a redirect to a login page, or a 401/403), `sync` stops and tells you to
   run `login <site>`.

Some sites only accept their own signed headers, so the fast path can't call their API directly. Zepto and
Blinkit are like this. Their recipes are saved with `fast_path_ok: false`, and `sync` goes straight to the
replay. The replay scrolls the order list and clicks "Load More" / "Show more" / "Next" when the list has
one (Zepto). With a numbered pager (Zomato: 1 2 3 … 18), it clicks the next page number in sequence, never
just any "2".

`sync all` runs each site that already has a recipe in its own worker process, with its own Chrome and
profile, 4 at a time (`--workers`). The 1–2 s pause between pages applies within each site. A site that
still needs `learn`, or whose worker fails (for example because you're logged out), runs afterwards in
the main process, where it can prompt you. With all 4 sites ready, a full sync takes about 90 s.

`learn` checks pagination before saving a recipe. It fetches page 2, and if page 2 returns page 1's
orders, the endpoint is ignoring the page parameter. That's how Zomato's page JSON fooled it at first;
Zomato's pager actually calls `/webroutes/user/orders?page=N`. The recipe is rejected, and the error tells
the LLM to use the request the site makes for "next page". Validation errors also report, field by field,
what the mapping extracted from the first order. Every candidate response comes with an "order-shape"
hint: the path to each order-like collection and one full example item. That way the LLM never maps
fields from a truncated response.

### Seeds: known endpoints as data

`recipes/<site>.seed.json` holds an endpoint and its pagination that are already known, but no field
mapping. When a seed exists, `learn` skips browsing. It fetches page 1 inside your logged-in tab, has the
LLM write the `field_map` from that real response, and validates it like any other recipe. Swiggy ships
with one: `GET /dapi/order/all`, paged with `?order_id=<last order id>`. That's the endpoint used by
[this Swiggy expense tracker](https://www.ashutoshshukla.in/posts/how-i-built-swiggy-expense-tracker-chrome-extension/).
Delete the seed file to fall back to discovery.

Zomato's bare `zomato.com` is now an app-download page. The Zomato session starts at
`https://www.zomato.com/<city>/restaurants` instead, where `<city>` comes from your saved address
(`Bengaluru` becomes `bangalore`, and the Delhi NCR cities become `ncr`).

### Resetting a recipe

```bash
python cli.py reset blinkit     # or delete recipes/blinkit.json
python cli.py learn blinkit
```

The orders-page hint (`recipes/<site>.hint.txt`) stays where it is. Run `login` again to change it.
To clear a login, delete `profiles/<site>`.

## Data

`expenses.db` (SQLite):

- **`orders`**: `site`, `order_id` (primary key together with `site`), `sub_platform` (`instamart` for
  Instamart), `placed_at` (ISO), `total_amount`, `currency`, `status` (normalized to `delivered`,
  `cancelled`, `refunded`, `failed`, or the raw value in lowercase), `merchant`, `raw_json`.
- **`order_items`**: `site`, `order_id`, `idx`, `name`, `qty`, `price`. The price is the line total.
- **`item_categories`**: the category cache, keyed by normalized item name.

Upserts are idempotent: running `sync` again updates existing orders and replaces their items.

Reports leave out cancelled, refunded and failed orders. Zomato and Swiggy-food items always count as
`food delivery` and are never sent to the LLM. Category totals are summed from item prices, so they
won't exactly match order totals, which include fees and discounts. Zepto's and Blinkit's order lists
carry no item prices. For those orders, the order total is split across the items by quantity, so the
category is an estimate but the total adds up.

## Analyst agent

`months` prints two pivots, month × platform and month × category, using the same rules as `report`
(cancelled, refunded and failed orders are left out). It writes them to `reports/monthly_platform.csv`
and `reports/monthly_category.csv`.

`ask` and `insights` use an LLM analyst that answers only from your database. It may call `run_sql`,
which accepts a single `SELECT`/`WITH` query and runs it on a connection opened read-only
(`mode=ro`). It can never change the data. `insights` asks it for a month-wise breakdown: totals,
per-platform amounts, change from the previous month, top restaurants and stores, the category split,
and recent changes.

## Server (local)

The multi-user backend uses Postgres (Supabase in production). The schema is in
`supabase/migrations/`. To run the server tests against a throwaway Postgres:

```bash
docker run -d --name ea-pg -p 54329:5432 -e POSTGRES_PASSWORD=postgres postgres:16
export TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:54329/postgres
pytest tests/server -v
```

The tests apply `tests/server/bootstrap.sql` (stand-ins for Supabase's `auth.uid()` and roles) and
then the migrations. Without `TEST_DATABASE_URL` the server tests are skipped. They **drop and
recreate** the `public` and `auth` schemas, so they refuse to run unless the host is `localhost` /
`127.0.0.1` (set `ALLOW_DESTRUCTIVE_TEST_DB=1` only for a database you are happy to wipe).

To run the API and the worker locally, first give them their own database (not the test one, which
the tests wipe) with the bootstrap and every migration applied in order. `bootstrap.sql` is for a
local plain Postgres only - never apply it to Supabase, which already has the `auth` schema and roles:

```bash
docker exec ea-pg psql -U postgres -c "create database ea_dev"
for f in tests/server/bootstrap.sql supabase/migrations/*.sql; do
  docker exec -i ea-pg psql -U postgres -d ea_dev -v ON_ERROR_STOP=1 < "$f"
done
export DATABASE_URL=postgresql://postgres:postgres@localhost:54329/ea_dev
```

On Supabase apply only `supabase/migrations/*.sql`, in order.

Then run the API and the worker (one process each; set `DATABASE_URL` and `SUPABASE_JWT_SECRET`):

```bash
uvicorn server.api:app
python -m server.worker
```

The worker's `DATABASE_URL` must be a direct or session-mode connection: it waits for OTPs with
`LISTEN`, which Supabase's transaction pooler (port 6543) does not support. Use the direct
connection (port 5432) or the session pooler.

The worker claims `connect` and `sync` jobs and relays OTPs sent to `POST /jobs/<id>/otp`. Each
user's Chrome profile is stored encrypted in the vault at `PROFILE_STORE_URL` (default
`file://vault_local`; an Azure Storage connection string in the cloud). For a job the worker
restores it into a per-job directory `PROFILE_ROOT/<job id>/` (default `profiles_users/`), and
deletes that plaintext directory when the job ends (and any leftovers at start-up).

Locally the worker refuses to start without a key: set `PROFILE_MASTER_KEY` (32 bytes, base64), or
`ALLOW_DEV_PROFILE_KEY=1` to use the fixed development key with a `file://` vault. That key is in
this repository, so dev-key blobs are **not secret**: anyone with the `vault_local/` files can
decrypt them. Use it only for test accounts, and never with a non-`file://` vault (refused anyway).

```bash
export ALLOW_DEV_PROFILE_KEY=1          # or: export PROFILE_MASTER_KEY=<base64 of 32 random bytes>
python -m server.worker
```

`MAX_JOB_S` (default 1200 s) is the longest a job may run. A job still running after that is presumed
hung: its heartbeat stops and the worker process exits (non-zero), so the container restarts and the
stale job is reclaimed. In-page API calls time out after 60 s. `STALE_JOB_S` (default 300 s) is how
long a running job may go without a heartbeat (every 30 s) before it is requeued (a sync) or failed
with `worker_lost` (a connect).

On `SIGTERM` the worker drains: it claims no new job and lets the current one finish for up to
`DRAIN_S` (default 540 s, inside Azure's 600 s grace period). A job still running then is aborted: a
sync goes straight back to the queue, a connect fails with `worker_shutdown`. Ctrl-C aborts at once.

### Scheduler, limits and budget

`python -m server.scheduler` (compose service `scheduler`; an Azure Container Apps job every 15 minutes with
`--once`) queues a `sync` for each connection not synced in `SYNC_INTERVAL_H` hours (24), skipping connections that
need login and sites whose orders recipe is unhealthy or paused. It also sends budget alerts to admins (once per month
at `BUDGET_ALERT_PCT`, again at 100 %) and dispatches pending notifications. Once the monthly budget is reached,
automatic syncs slow to every `PAUSED_SYNC_INTERVAL_H` hours (72). Set `MONTHLY_BUDGET_INR`, `PROXY_INR_PER_GB`,
`AI_INR_PER_1K_TOKENS`, `WORKER_INR_PER_MINUTE`, `FIXED_MONTHLY_INR`, `REFRESH_PER_APP_PER_DAY` and
`CART_CHECKS_PER_DAY` for the API, worker and scheduler alike. Create the first admin with
`python cli.py add-admin you@example.com`.

### Data controls and hardening tunables

Users control their data on the Accounts tab: **Disconnect an app** deletes its session and orders, and **Delete
account** removes everything (type `DELETE`), including the sign-in. The public `/privacy` page says what is read,
stored and who sees it. `python cli.py verify-erased <user id> <email>` proves nothing of a deleted user remains
(`NOTHING REMAINS`).

A site that rate-limits us goes into a cool-down (`BACKOFF_BASE_S` 60, doubling up to `BACKOFF_MAX_S` 3600, reset after
`BACKOFF_RESET_H` 24 quiet hours); refresh and fill answer 503 with `Retry-After` meanwhile. The error budget pauses a
site automatically when at least `ERROR_BUDGET_MIN_JOBS` (10) jobs in the last `ERROR_BUDGET_WINDOW_H` (24) hours
finished and more than `ERROR_BUDGET_MAX_FAILURE_PCT` (30) percent failed; admins are emailed and resume it from the
Sites tab. Before deploying, follow `docs/deploy-checklist.md`; the local full-product run is
`bash scripts/e2e_local.sh` (`docs/e2e-local.md`).

## Tests

```bash
pytest
```

The tests cover:
- the guard rules, including bot-challenge POSTs;
- field_map extraction on fixture JSON, dates without a year, and item lists written as one string;
- pagination (page, offset and cursor, in the query string and in a POST body), and the page-2 check;
- idempotent DB upserts;
- the login helpers: OTP masking, refusing to finish early or unverified, and ask-user;
- the monthly pivot and the read-only SQL guard.
